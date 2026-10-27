"""A real user's platform handoff: only commands, no hand-written contract.

5.1.9 rebound the unfinished nodes only when the capability contract had
changed, but nothing a user can run ever changes it (``init`` writes it once
with ``provider=unknown``; ``attest`` writes only the provider mailbox).  The
earlier tests got there by calling ``save_contract`` themselves, a step a real
user cannot take.  These tests use ``run_cli`` and nothing else.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_platform_handoff_rebind import (
    FIRST, PLAN, SECOND, HandoffCase, _read,
)
from vibe_guide.capability_contract import build_contract, load_contract
from vibe_guide.handoff_rebind import HandoffRebindError, contract_for_platform, resolve_handoff_target
from vibe_guide.state import load_events

CONTRACT = ".vibe/session-contract.json"


class RealUserHandoffTests(HandoffCase):
    def contract_text(self):
        return (self.root / CONTRACT).read_text(encoding="utf-8")

    def ready(self):
        snapshot = self.published_on("workbuddy")
        self.assertEqual(snapshot.nodes[FIRST]["status"], "accepted")
        self.assertEqual(snapshot.nodes[SECOND]["status"], "running")
        return snapshot

    def assert_moved_and_finishes(self, before):
        after = self.snapshot()
        self.assertNotEqual(after.nodes[SECOND]["status"], "blocked_unknown", after.nodes[SECOND])
        self.assertEqual([(a["issue_id"], a["provider"]) for a in self.store.pending()],
                         [(SECOND, "claude-code-visible")])
        self.serve(SECOND)
        done = self.snapshot()
        self.assertEqual(done.nodes[SECOND]["status"], "accepted")
        self.assertEqual(done.nodes[FIRST]["acceptance"]["contract_digest"],
                         before.nodes[FIRST]["acceptance"]["contract_digest"])
        self.assertEqual(done.nodes[FIRST]["developer_identity"], before.nodes[FIRST]["developer_identity"])
        nodes = {n["id"]: n for n in _read(self.root / ".vibe" / "plans" / PLAN / "nodes.json")}
        self.assertEqual(nodes[SECOND]["contract"]["adapter_id"], "claude-code")
        self.assertEqual(nodes["integration-review"]["contract"]["adapter_id"], "claude-code")
        self.assertEqual(nodes[FIRST]["contract"]["adapter_id"], "workbuddy")
        self.assertEqual(self.plan_card().agent_id, "claude-code")

    def test_attest_then_handoff_with_adapter_moves_the_run(self):
        before = self.ready()
        self.attest("claude-code")
        result = self.handoff("--adapter", "claude-code")
        self.assertNotEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assert_moved_and_finishes(before)

    def test_attest_then_handoff_without_adapter_moves_the_run(self):
        before = self.ready()
        self.attest("claude-code")
        result = self.handoff()
        self.assertNotEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assert_moved_and_finishes(before)

    def test_the_handoff_refreshes_the_session_contract_to_the_new_platform(self):
        self.ready()
        self.assertEqual(load_contract(self.paths).provider, "unknown")
        self.attest("claude-code")
        self.assertEqual(load_contract(self.paths).provider, "unknown")  # attest has no side effect
        self.handoff("--adapter", "claude-code")
        contract = load_contract(self.paths)
        self.assertEqual(contract.provider, "claude-code")
        self.assertEqual(self.snapshot().capability_contract_digest, contract.contract_digest)

    def test_no_command_but_the_handoff_rewrites_the_contract(self):
        self.ready()
        before = self.contract_text()
        self.attest("claude-code")
        self.cli("resume", "--plan", PLAN)
        self.cli("status")
        self.cli("monitor", "--plan", PLAN, "--authorize", "AUTHORIZE")
        self.assertEqual(self.contract_text(), before)

    def test_without_adapter_and_two_candidates_it_stops_and_changes_nothing(self):
        self.ready()
        self.attest("claude-code")
        self.attest("codex")
        before = self.contract_text()
        nodes = (self.root / ".vibe" / "plans" / PLAN / "nodes.json").read_text(encoding="utf-8")
        result = self.handoff()
        self.assertEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertIn("--adapter", result.payload["reason"])
        self.assertIn("claude-code", result.payload["reason"])
        self.assertIn("codex", result.payload["reason"])
        self.assertEqual(self.contract_text(), before)
        self.assertEqual((self.root / ".vibe" / "plans" / PLAN / "nodes.json").read_text(encoding="utf-8"), nodes)
        self.assertEqual(len([e for e in load_events(self.paths, self.run_id)
                              if e["event"] == "authorization_reauthorized"]), 0)

    def test_an_attested_platform_with_no_dispatch_lifecycle_is_not_a_candidate(self):
        """grok attests but never becomes dispatchable: it must not make a tie."""
        before = self.ready()
        self.attest("claude-code")
        self.attest("grok")
        result = self.handoff()
        self.assertNotEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertEqual(load_contract(self.paths).provider, "claude-code")
        self.assert_moved_and_finishes(before)

    def test_a_finished_handoff_rerun_without_adapter_is_a_quiet_no_op(self):
        before = self.ready()
        self.attest("claude-code")
        self.handoff("--adapter", "claude-code")
        again = self.handoff()
        self.assertNotEqual(again.payload["status"], "blocked_unknown", again.payload)
        self.assertEqual(load_contract(self.paths).provider, "claude-code")
        self.assert_moved_and_finishes(before)

    def test_an_unstated_contract_with_no_other_platform_is_not_a_stranded_run(self):
        self.ready()
        result = self.handoff()
        self.assertNotEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertEqual(load_contract(self.paths).provider, "unknown")

    def test_a_contract_naming_another_platform_than_the_nodes_is_a_readable_stop(self):
        """CONSTRUCTED STATE (users cannot reach it with 5.1.10 commands).

        Only a contract written by hand, as 5.1.9-era supervisors did, names a
        platform while unfinished nodes still sit on another and nothing can
        name the target.  The handoff must say so instead of re-authorizing
        into a silent no-op.  The contract is written with build/save here on
        purpose and this test claims nothing about a real user path.
        """
        from vibe_guide.capability_contract import build_contract, save_contract

        self.ready()
        save_contract(self.paths, build_contract(self.root, provider="claude-code", host_id="local"))
        plan = self.root / ".vibe" / "plans" / PLAN
        files = {name: (plan / name).read_bytes() for name in ("nodes.json", "authorization-card.json", "authorization.json")}
        contract = self.contract_text()
        result = self.handoff()
        self.assertEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertIn("--adapter", result.payload["reason"])
        self.assertIn("claude-code", result.payload["reason"])
        for name, content in files.items():
            self.assertEqual((plan / name).read_bytes(), content, name)
        self.assertEqual(self.contract_text(), contract)
        self.assertEqual(len([e for e in load_events(self.paths, self.run_id)
                              if e["event"] == "authorization_reauthorized"]), 0)

    def test_an_unattested_target_leaves_the_contract_alone(self):
        self.ready()
        before = self.contract_text()
        result = self.handoff("--adapter", "claude-code")
        self.assertEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertEqual(self.contract_text(), before)

    def test_rerunning_a_finished_handoff_converges(self):
        before = self.ready()
        self.attest("claude-code")
        self.handoff("--adapter", "claude-code")
        text = self.contract_text()
        again = self.handoff("--adapter", "claude-code")
        self.assertNotEqual(again.payload["status"], "blocked_unknown", again.payload)
        self.assertEqual(self.contract_text(), text)
        self.assert_moved_and_finishes(before)


class ContractRefreshCrashWindowTests(HandoffCase):
    """Crashes between the contract refresh and the other handoff writes."""

    contract_text = RealUserHandoffTests.contract_text
    ready = RealUserHandoffTests.ready
    assert_moved_and_finishes = RealUserHandoffTests.assert_moved_and_finishes

    def crash_on_event(self):
        from vibe_guide.monitor import Monitor

        real = Monitor._record

        def crash(self_, snapshot, event_name, *args, **kwargs):
            if event_name == "authorization_reauthorized":
                raise RuntimeError("injected crash before the event")
            return real(self_, snapshot, event_name, *args, **kwargs)

        return patch.object(Monitor, "_record", crash)

    def test_a_failure_before_the_event_restores_the_contract_and_a_rerun_converges(self):
        before = self.ready()
        self.attest("claude-code")
        original = self.contract_text()
        with self.crash_on_event():
            interrupted = self.handoff("--adapter", "claude-code")
        self.assertEqual(interrupted.payload["status"], "unknown", interrupted.payload)
        self.assertEqual(self.contract_text(), original)
        self.assertEqual(self.snapshot().authorization["agent_id"], "workbuddy")
        rerun = self.handoff("--adapter", "claude-code")
        self.assertNotEqual(rerun.payload["status"], "blocked_unknown", rerun.payload)
        self.assert_moved_and_finishes(before)

    def test_a_hard_kill_after_the_refresh_converges_even_without_adapter(self):
        from vibe_guide import cli as cli_module

        before = self.ready()
        self.attest("claude-code")
        with self.crash_on_event(), patch.object(
            cli_module, "_atomic_bytes", side_effect=OSError("killed before rollback")
        ):
            self.handoff("--adapter", "claude-code")
        self.assertEqual(load_contract(self.paths).provider, "claude-code")
        self.assertEqual(self.snapshot().authorization["agent_id"], "workbuddy")
        rerun = self.handoff()
        self.assertNotEqual(rerun.payload["status"], "blocked_unknown", rerun.payload)
        self.assert_moved_and_finishes(before)

    def test_a_crash_between_nodes_and_card_converges(self):
        from vibe_guide import cli as cli_module

        before = self.ready()
        self.attest("claude-code")
        real = cli_module._atomic_json

        def crash(path, payload):
            if Path(path).name == "authorization-card.json":
                raise OSError("injected crash before the card")
            return real(path, payload)

        with patch.object(cli_module, "_atomic_json", crash):
            self.handoff("--adapter", "claude-code")
        self.assertEqual(load_contract(self.paths).provider, "claude-code")
        rerun = self.handoff()
        self.assertNotEqual(rerun.payload["status"], "blocked_unknown", rerun.payload)
        self.assert_moved_and_finishes(before)

    def test_a_rerun_through_the_invalidation_marker_branch_refreshes_the_contract(self):
        """The marker branch is reachable by commands alone.

        A handoff killed after the reauthorization committed but before
        nodes.json was written leaves the card naming a platform the files
        do not; the user's next plain ``resume`` then writes
        ``authorization-invalidated.json``.  Re-running the handoff takes the
        other card-refresh branch, and it must refresh the contract too.
        """
        from vibe_guide import cli as cli_module

        before = self.ready()
        self.attest("claude-code")
        real = cli_module._atomic_json

        def crash(path, payload):
            if Path(path).name == "nodes.json":
                raise OSError("injected kill before nodes.json")
            return real(path, payload)

        with patch.object(cli_module, "_atomic_json", crash), patch.object(
            cli_module, "_atomic_bytes", side_effect=OSError("killed before rollback")
        ):
            self.handoff("--adapter", "claude-code")
        marker = self.root / ".vibe" / "plans" / PLAN / "authorization-invalidated.json"
        self.assertFalse(marker.exists())
        self.cli("resume", "--plan", PLAN)
        self.assertTrue(marker.exists(), "the plain resume after the kill must leave the marker")
        # The killed run had already committed the contract; the re-run goes
        # through the marker branch and must leave it consistent.
        self.assertEqual(load_contract(self.paths).provider, "claude-code")
        rerun = self.handoff("--adapter", "claude-code")
        self.assertNotEqual(rerun.payload["status"], "blocked_unknown", rerun.payload)
        self.assertFalse(marker.exists())
        self.assertEqual(load_contract(self.paths).provider, "claude-code")
        self.assert_moved_and_finishes(before)

    def test_an_acceptance_change_alongside_the_handoff_leaves_the_contract_untouched(self):
        self.ready()
        self.attest("claude-code")
        path = self.root / ".vibe" / "plans" / PLAN / "nodes.json"
        items = _read(path)
        for item in items:
            if item["id"] == SECOND:
                item["contract"]["acceptance_example"] = "verified_fact: 改过的验收例子"
        path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        original = self.contract_text()
        result = self.handoff("--adapter", "claude-code")
        self.assertEqual(result.payload["status"], "blocked_design", result.payload)
        self.assertEqual(self.contract_text(), original)


class PureRuleTests(unittest.TestCase):
    def resolve(self, **overrides):
        args = dict(explicit=None, card_agent="a", run_agent="a", run_drift=False,
                    capability_changed=False, contract_provider="unknown", dispatchable_others=())
        args.update(overrides)
        return resolve_handoff_target(**args)

    def test_an_unstated_contract_provider_lets_the_one_other_platform_be_the_target(self):
        self.assertEqual(self.resolve(dispatchable_others=("a", "b")), "b")

    def test_an_unstated_contract_with_several_candidates_is_an_error_not_a_pick(self):
        with self.assertRaises(HandoffRebindError) as caught:
            self.resolve(dispatchable_others=("a", "b", "c"))
        self.assertIn("--adapter", str(caught.exception))

    def test_a_stated_contract_provider_and_no_change_means_nothing_moves(self):
        self.assertIsNone(self.resolve(contract_provider="b", dispatchable_others=("a", "b")))

    def test_the_contract_validity_is_renewed_but_each_fact_keeps_its_own_clock(self):
        import tempfile
        from datetime import datetime, timedelta, timezone

        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        past = datetime.now(timezone.utc) - timedelta(minutes=30)
        old = build_contract(root, host_id="h1", now=past, facts={
            "x": {"status": "verified_available", "scope": "task", "route": "r",
                  "evidence_ref": "e", "checked_at": "2020-01-01T00:00:00Z",
                  "expires_at": "2020-01-02T00:00:00Z"}})
        new = contract_for_platform(old, root, "claude-code")
        self.assertGreater(new.expires_at, old.expires_at)
        self.assertNotEqual(new.contract_digest, old.contract_digest)
        self.assertTrue(old.capabilities["x"].checked_at.startswith("2020-01-01T00:00:00"))
        self.assertEqual(new.capabilities["x"].checked_at, old.capabilities["x"].checked_at)
        self.assertEqual(new.capabilities["x"].expires_at, old.capabilities["x"].expires_at)
        for field in ("schema_version", "project_digest", "host_id", "scope"):
            self.assertEqual(getattr(new, field), getattr(old, field), field)

    def test_contract_for_platform_changes_only_the_provider(self):
        import tempfile

        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        old = build_contract(root, host_id="h1", facts={"x": {"status": "unknown"}})
        new = contract_for_platform(old, root, "claude-code")
        self.assertEqual((new.provider, new.host_id), ("claude-code", "h1"))
        self.assertEqual(new.capabilities, old.capabilities)
        self.assertIsNone(contract_for_platform(new, root, "claude-code"))


class HandoffDocumentationTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def section(self, name, heading):
        text = (self.ROOT / name).read_text(encoding="utf-8")
        start = text.index(heading)
        end = text.find("\n## ", start)
        return text[start: end if end != -1 else len(text)]

    def test_both_readmes_document_the_handoff_in_its_own_section(self):
        for name, heading, plan in (("README.md", "### 换平台交接", "<计划>"),
                                    ("README.en.md", "### Platform handoff", "<plan>")):
            body = self.section(name, heading)
            new = "<新平台>" if name == "README.md" else "<new-platform>"
            for needle in ("vibe attest --adapter " + new,
                           "vibe monitor --plan {} --authorize AUTHORIZE --platform-handoff [--adapter {}]".format(plan, new)):
                self.assertIn(needle, body, (name, needle))

    def test_the_drift_hint_names_the_handoff(self):
        from vibe_guide.supervisor import CONTRACT_DRIFT_HINT

        self.assertIn("--platform-handoff", CONTRACT_DRIFT_HINT)


if __name__ == "__main__":
    unittest.main()
