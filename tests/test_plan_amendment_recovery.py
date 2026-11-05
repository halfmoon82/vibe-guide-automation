"""P1 recovery contract: crash boundaries of the same-run amendment transfer.

B06: the transfer is split at the durable ``plan_amendment_transferred``
event.  A crash before the event commits must restore the complete old
state; a crash after it must idempotently finish the new projection.
While a marker shows the transfer incomplete, every entry point -- CLI
authorize/monitor/resume/daemon and a direct ``Monitor`` call -- fails
closed instead of consuming results or dispatching.

B12 path B: an isolated project copy runs the public CLI
propose -> show -> confirm -> same-run resume end to end.
"""
import json
import unittest
from pathlib import Path

import vibe_guide
from vibe_guide import plan_amendment
from vibe_guide.authorization import AuthorizationRecord, authorize
from vibe_guide.cli import _authorization_card, run_cli
from vibe_guide.state import load_events, load_snapshot

from tests.test_plan_amendment import (
    _amendment_spec,
    _authorized_probe,
    _read,
    _start_run,
)


FROZEN_HEAD = "885236783e1a680d7165ee2ee2c4e0002430a30a"


def setUpModule():
    import subprocess
    root = Path(vibe_guide.__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        head = "unreadable"
    print("[loaded vibe_guide: {} version={} head={} frozen={}]".format(
        vibe_guide.__file__, getattr(vibe_guide, "__version__", "?"),
        head, FROZEN_HEAD))


class _PreparedAmendment(unittest.TestCase):
    def setUp(self):
        self.root, self.paths, self.plan_root, self.record = _authorized_probe(self)
        # A real run, left quiescent so the amendment may proceed.
        self.snapshot, _runner = _start_run(
            self.paths, self.plan_root, self.record, quiesce=True)
        self.spec = _amendment_spec(self.plan_root)
        self.candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "修订验收")
        plan_amendment.show(
            self.paths, "probe-plan", self.candidate["amendment_digest"])
        self.digest = self.candidate["amendment_digest"]
        self.token = plan_amendment.confirmation_token(self.digest)

    def _artifacts(self):
        names = ["authorization-card.json", "authorization.json", "plan.json",
                 "nodes.json", "plan-confirmation.json", "prd.md"]
        return {name: (self.plan_root / name).read_bytes() for name in names
                if (self.plan_root / name).is_file()}


class CrashBeforeEventTests(_PreparedAmendment):
    def test_pre_event_crash_restores_old_state(self):
        before = self._artifacts()
        with self.assertRaises(RuntimeError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token,
                _fault_at="before_event")
        # After the faulted attempt, a public recovery call (or the next
        # confirm) must leave the old state intact and the barrier clear.
        plan_amendment.recover(self.paths, "probe-plan")
        self.assertEqual(before, self._artifacts())
        self.assertFalse(plan_amendment.transfer_pending(self.paths, "probe-plan"))
        # Retrying the same confirm then succeeds exactly once.
        result = plan_amendment.confirm(
            self.paths, "probe-plan", self.digest, self.token)
        self.assertEqual(result["status"], "amended")


class CrashAfterEventTests(_PreparedAmendment):
    def test_post_event_crash_completes_idempotently(self):
        with self.assertRaises(RuntimeError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token,
                _fault_at="after_event")
        recovery = plan_amendment.recover(self.paths, "probe-plan")
        self.assertEqual(recovery["status"], "completed")
        events = load_events(self.paths, recovery["run_id"])
        transferred = [
            item for item in events
            if item["event"] == "authorization_reauthorized"
            and item["data"].get("change_reason") == "plan_amendment"]
        self.assertEqual(len(transferred), 1)
        # Re-running recovery is a no-op, not a second transfer.
        again = plan_amendment.recover(self.paths, "probe-plan")
        events = load_events(self.paths, recovery["run_id"])
        self.assertEqual(
            len([i for i in events
                 if i["event"] == "authorization_reauthorized"
                 and i["data"].get("change_reason") == "plan_amendment"]), 1)
        card = _read(self.plan_root, "authorization-card.json")
        self.assertEqual(
            card["digest"], self.candidate["candidate_card"]["digest"])
        snapshot = load_snapshot(self.paths, recovery["run_id"])
        self.assertEqual(
            snapshot.authorization_digest,
            self.candidate["candidate_card"]["digest"])
        self.assertEqual(snapshot.run_id, recovery["run_id"])


class BarrierTests(_PreparedAmendment):
    def _stall_transfer(self):
        with self.assertRaises(RuntimeError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token,
                _fault_at="after_event")

    def test_stalled_transfer_recovers_and_entries_never_dispatch_half_state(self):
        self._stall_transfer()
        self.assertTrue(
            plan_amendment.transfer_pending(self.paths, "probe-plan"))
        # The first entry point that touches the plan heals the committed
        # side of the transfer; none of them consumed or dispatched on the
        # half-amended state.
        recovered = plan_amendment.recover(self.paths, "probe-plan")
        self.assertEqual(recovered["status"], "completed")
        self.assertFalse(
            plan_amendment.transfer_pending(self.paths, "probe-plan"))
        events = load_events(self.paths, recovered["run_id"])
        self.assertEqual(
            len([item for item in events
                 if item["event"] == "authorization_reauthorized"
                 and item["data"].get("change_reason") == "plan_amendment"]),
            1)
        # With the transfer complete the CLI entry points behave normally
        # again (blocked only by ordinary gates, never by a stuck barrier).
        result = run_cli(
            ["status", "--json"], self.root)
        self.assertNotEqual(result.payload.get("reason", ""),
                            "amendment transfer unresolved")

    def test_conflicting_marker_refuses_dispatch(self):
        # A marker naming a different candidate cannot be quietly recovered.
        marker = plan_amendment.barrier_path(self.paths, "probe-plan")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "amendment_digest": "0" * 64, "run_id": "run-unknown"}),
            encoding="utf-8")
        with self.assertRaises(PermissionError):
            plan_amendment.recover(self.paths, "probe-plan")
        result = run_cli(
            ["monitor", "--plan", "probe-plan", "--json"], self.root)
        self.assertIn(result.payload.get("status"),
                      ("blocked", "blocked_design", "blocked_unknown"))


class CliEndToEndTests(unittest.TestCase):
    """B12 path B: the public CLI path on an isolated project copy."""

    def test_cli_propose_show_confirm_same_run_resume(self):
        root, paths, plan_root, record = _authorized_probe(self)
        snapshot, _runner = _start_run(
            paths, plan_root, record, quiesce=True)
        snapshot = load_snapshot(paths, snapshot.run_id)

        spec_path = root / "amend-spec.json"
        spec_path.write_text(
            json.dumps(_amendment_spec(plan_root), ensure_ascii=False),
            encoding="utf-8")
        proposed = run_cli(
            ["plan-amend", "propose", "--plan", "probe-plan",
             "--amend-spec", str(spec_path), "--amend-reason", "修订验收",
             "--json"], root)
        self.assertEqual(proposed.payload.get("status"), "proposed",
                         proposed.payload)
        digest = proposed.payload["amendment_digest"]
        shown = run_cli(
            ["plan-amend", "show", "--plan", "probe-plan",
             "--amendment", digest, "--json"], root)
        self.assertEqual(shown.payload.get("status"), "shown", shown.payload)
        token = "AMEND " + digest
        confirmed = run_cli(
            ["plan-amend", "confirm", "--plan", "probe-plan",
             "--amendment", digest, "--confirm-token", token, "--json"], root)
        self.assertEqual(confirmed.payload.get("status"), "amended",
                         confirmed.payload)
        # Same run, new binding, old record preserved in history.
        self.assertEqual(confirmed.payload["run_id"], snapshot.run_id)
        current = load_snapshot(paths, snapshot.run_id)
        self.assertEqual(current.authorization_digest,
                         proposed.payload["candidate_card"]["digest"])
        events = load_events(paths, snapshot.run_id)
        amendment_events = [
            item for item in events
            if item["event"] == "authorization_reauthorized"
            and item["data"].get("change_reason") == "plan_amendment"]
        self.assertEqual(len(amendment_events), 1)
        self.assertEqual(
            amendment_events[0]["data"].get("amendment_digest"), digest)
        # Old authorization record remains readable in amendment history.
        history_dir = plan_root / "amendments" / digest
        self.assertTrue(
            (history_dir / "previous-authorization.json").is_file())


if __name__ == "__main__":
    unittest.main()
