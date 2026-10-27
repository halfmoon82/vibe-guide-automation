"""A handoff leaves exactly one pending create per node, whatever its history.

5.1.11 hid the old platform's unclaimed create only inside the lease-rebind
path, which a node reaches only while it still holds the old writer lease.  A
dispatch that failed once (``contract_error``) releases that lease, so the next
handoff took the new-lease path and the mailbox kept two pending creates for
one node.  Every test drives ``run_cli`` only; the one injected fault and the
one hand-removed marker are named in their docstrings.
"""
import json
from pathlib import Path
from unittest.mock import patch

from tests.test_platform_handoff_rebind import FIRST, PLAN, SECOND, HandoffCase
from vibe_guide.monitor import Monitor
from vibe_guide.state import load_events


class StaleCreateTests(HandoffCase):
    def stale_dir(self):
        return self.root / ".vibe" / "provider-actions" / "stale"

    def stale_ids(self):
        return sorted(p.stem for p in self.stale_dir().glob("action-*.json")) if self.stale_dir().is_dir() else []

    def events(self, name):
        return [e for e in load_events(self.paths, self.run_id) if e["event"] == name]

    def fail_the_dispatch_once(self):
        """CONSTRUCTED STATE: inject one dispatch failure under the old platform.

        The real-project evidence (a 5.1.9 handoff) shows ``contract_error``
        with the node's writer lease released and the old create still
        unclaimed.  Nothing a user types makes a dispatch fail on demand, so
        ``_binding_for`` is made to raise once during an ordinary
        ``resume`` (and the lease re-acquire that a plain
        re-run would otherwise stop at is let through), so the real
        ``_preflight_contract_failure`` path the evidence went through runs
        and releases the lease.
        """
        real = Monitor._binding_for
        state = {"raised": 0}

        def boom(self_, *args, **kwargs):
            if not state["raised"]:
                state["raised"] += 1
                raise ValueError("injected binding failure")
            return real(self_, *args, **kwargs)

        with patch.object(Monitor, "_binding_for", boom), patch(
            "vibe_guide.monitor.acquire_writer_lease", return_value=True
        ):
            self.cli("resume", "--plan", PLAN)
        return state["raised"]

    def assert_one_pending_on(self, provider):
        pending = [(a["issue_id"], a["provider"]) for a in self.store.pending()]
        self.assertEqual(pending, [(SECOND, provider)], pending)

    def finish_on_new_platform(self, before):
        self.serve(SECOND)
        done = self.snapshot()
        self.assertEqual(done.nodes[SECOND]["status"], "accepted")
        self.assertEqual(done.nodes[FIRST]["acceptance"]["contract_digest"],
                         before.nodes[FIRST]["acceptance"]["contract_digest"])

    def test_start_a_never_failed_leaves_one_pending_create(self):
        before = self.published_on("workbuddy")
        old = [a["action_id"] for a in self.store.pending()]
        self.attest("claude-code")
        self.handoff("--adapter", "claude-code")
        self.assert_one_pending_on("claude-code-visible")
        self.assertEqual(self.stale_ids(), sorted(old))
        self.finish_on_new_platform(before)

    def test_start_b_failed_once_with_lease_released_leaves_one_pending_create(self):
        before = self.published_on("workbuddy")
        old = [a["action_id"] for a in self.store.pending()]
        self.assertEqual(self.fail_the_dispatch_once(), 1)
        self.assertTrue(self.events("contract_error"), "the injected failure must have run the real failure path")
        self.assertEqual(list((self.root / ".vibe" / "leases").glob("*")) and [
            p for p in (self.root / ".vibe" / "leases").glob("*") if SECOND in p.name], [],
            "the failure path must have released the node's lease")
        self.attest("claude-code")
        self.handoff("--adapter", "claude-code")
        self.assert_one_pending_on("claude-code-visible")
        self.assertEqual(self.stale_ids(), sorted(old))
        self.finish_on_new_platform(before)

    def test_rerunning_the_handoff_heals_a_node_left_with_two_pending_creates(self):
        """CONSTRUCTED STATE: the 5.1.11 result, reached by removing one marker.

        5.1.11 on a failed-once project left the old create unhidden beside
        the new one (two pending, ``stale/`` empty; see the real project
        evidence).  Deleting the audit marker the handoff wrote reproduces
        exactly that mailbox shape on top of a real handoff.
        """
        before = self.published_on("workbuddy")
        old = [a["action_id"] for a in self.store.pending()]
        self.attest("claude-code")
        self.handoff("--adapter", "claude-code")
        for name in self.stale_ids():
            (self.stale_dir() / (name + ".json")).unlink()
        self.assertEqual(len(self.store.pending()), 2)
        self.handoff()
        self.assert_one_pending_on("claude-code-visible")
        self.assertEqual(self.stale_ids(), sorted(old))
        again = self.handoff()
        self.assertNotEqual(again.payload["status"], "blocked_unknown", again.payload)
        self.assert_one_pending_on("claude-code-visible")
        self.finish_on_new_platform(before)

    def test_without_the_flag_nothing_is_hidden(self):
        self.published_on("workbuddy")
        self.fail_the_dispatch_once()
        self.attest("claude-code")
        self.cli("resume", "--plan", PLAN)
        self.cli("monitor", "--plan", PLAN, "--authorize", "AUTHORIZE")
        self.assertEqual(self.stale_ids(), [])
        self.assertEqual([a["provider"] for a in self.store.pending()], ["workbuddy-visible"])

    def test_a_claimed_old_create_is_never_hidden(self):
        """CONSTRUCTED STATE: a completion lock marks the old create as claimed.

        That lock is what a session that picked the request up leaves behind
        (``ProviderActionStore.claim_evidence``); only a live desktop session
        writes it, so the test writes the file itself.
        """
        self.published_on("workbuddy")
        old = self.store.pending()[0]
        lock = self.root / ".vibe" / "provider-actions" / "locks" / (old["action_id"] + ".lock")
        lock.write_text("claimed", encoding="utf-8")
        self.attest("claude-code")
        self.handoff("--adapter", "claude-code")
        self.assertNotIn(old["action_id"], self.stale_ids())

    def test_a_plain_rerun_after_a_handoff_hides_nothing(self):
        """CONSTRUCTED STATE: the 5.1.11 two-pending mailbox, then no flag."""
        self.published_on("workbuddy")
        self.attest("claude-code")
        self.handoff("--adapter", "claude-code")
        for name in self.stale_ids():
            (self.stale_dir() / (name + ".json")).unlink()
        self.cli("monitor", "--plan", PLAN, "--authorize", "AUTHORIZE")
        self.cli("resume", "--plan", PLAN)
        self.assertEqual(self.stale_ids(), [])
        self.assertEqual(len(self.store.pending()), 2)

    def test_the_hide_is_audited_and_survives_the_event_filter(self):
        self.published_on("workbuddy")
        self.fail_the_dispatch_once()
        self.attest("claude-code")
        self.handoff("--adapter", "claude-code")
        events = self.events("handoff_stale_creates_hidden")
        self.assertEqual([e["data"]["node_id"] for e in events], [SECOND], events)
        self.assertEqual(sorted(events[0]["data"]["superseded_creates"]), self.stale_ids())
        self.assertEqual(events[0]["data"]["role"], "developer")

    def test_a_crash_right_after_hiding_converges_on_rerun(self):
        """The marker is written before the event; a crash between them must not strand the node."""
        before = self.published_on("workbuddy")
        self.fail_the_dispatch_once()
        self.attest("claude-code")
        real = Monitor._record

        def crash(self_, snapshot, event_name, *args, **kwargs):
            if event_name == "handoff_stale_creates_hidden":
                raise RuntimeError("injected crash after the stale marker")
            return real(self_, snapshot, event_name, *args, **kwargs)

        from vibe_guide import cli as cli_module

        with patch.object(Monitor, "_record", crash), patch.object(
            cli_module, "_atomic_bytes", side_effect=OSError("killed before rollback")
        ):
            self.handoff("--adapter", "claude-code")
        rerun = self.handoff()
        self.assertNotEqual(rerun.payload["status"], "blocked_unknown", rerun.payload)
        self.assert_one_pending_on("claude-code-visible")
        self.finish_on_new_platform(before)
