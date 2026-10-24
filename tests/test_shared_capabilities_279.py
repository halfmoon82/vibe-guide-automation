"""Issue #279: capability records are stored per adapter, never overwritten.

The shared ``capabilities.json`` used to hold exactly one adapter's record, so
a WorkBuddy attest rewrote the file a Claude Code monitor later read and
resume failed with "observed provider does not match".  Each adapter now owns
``capabilities-<adapter>.json``; the legacy file stays a mirror of the latest
record for pre-split readers, and reads resolve the adapter's own record --
never another adapter's facts.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore, ProviderPending
from vibe_guide.cli import run_cli
from vibe_guide.node_spec import observe_capabilities
from vibe_guide.paths import ProjectPaths

CLAUDE_FACTS = {
    "claude-code.agent": True,
    "claude-code.shell": True,
    "claude-code.subprocess": True,
    "claude-code.worktree": True,
    "claude-code.visible_task.create": True,
    "claude-code.visible_task.enter": True,
    "claude-code.visible_task.resume": True,
    "claude-code.visible_task.wait": True,
    "claude-code.in_session_sdd": False,
}
WORKBUDDY_FACTS = {
    "workbuddy.agent": True,
    "workbuddy.shell": True,
    "workbuddy.subprocess": True,
    "workbuddy.worktree": True,
    "workbuddy.visible_task.create": True,
    "workbuddy.visible_task.enter": True,
    "workbuddy.visible_task.resume": True,
    "workbuddy.visible_task.wait": True,
    "workbuddy.in_session_sdd": True,
}


class _Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v279-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)
        self.store = ProviderActionStore(self.paths)

    def init(self):
        result = run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(result.payload.get("status"), "ok", result.payload)

    def attest(self, adapter, facts, provenance="test session"):
        facts_path = self.root / "facts.json"
        facts_path.write_text(json.dumps(facts), encoding="utf-8")
        return run_cli(
            ["attest", "--json", "--adapter", adapter, "--facts", "facts.json",
             "--provenance", provenance],
            self.root,
        )


class PerAdapterStorageTests(_Case):
    def test_two_adapters_coexist_without_overwriting_each_other(self):
        self.init()
        self.assertEqual(
            self.attest("claude-code", CLAUDE_FACTS).payload.get("status"), "ok"
        )
        self.assertEqual(
            self.attest("workbuddy", WORKBUDDY_FACTS).payload.get("status"), "ok"
        )
        records = self.store.capabilities_all()
        self.assertEqual(sorted(records), ["claude-code", "workbuddy"])
        self.assertEqual(records["claude-code"]["facts"], CLAUDE_FACTS)
        self.assertEqual(records["workbuddy"]["facts"], WORKBUDDY_FACTS)

    def test_observe_reads_the_selected_adapter_after_another_attests(self):
        # The reported failure: a Claude Code monitor resume must not see the
        # WorkBuddy record the latest attest left in the legacy file.
        self.init()
        self.attest("claude-code", CLAUDE_FACTS)
        self.attest("workbuddy", WORKBUDDY_FACTS)
        observed = observe_capabilities(self.paths, "claude-code")
        self.assertEqual(observed.adapter_id, "claude-code")
        observed = observe_capabilities(self.paths, "workbuddy")
        self.assertEqual(observed.adapter_id, "workbuddy")

    def test_attest_summary_reports_the_adapter_it_just_recorded(self):
        self.init()
        self.attest("workbuddy", WORKBUDDY_FACTS)
        result = self.attest("claude-code", CLAUDE_FACTS)
        self.assertEqual(result.payload.get("adapter_id"), "claude-code")
        self.assertEqual(result.payload.get("facts"), CLAUDE_FACTS)


class MissingAndLegacyRecordTests(_Case):
    def test_missing_adapter_is_unregistered_never_borrowed(self):
        self.init()
        self.attest("workbuddy", WORKBUDDY_FACTS)
        with self.assertRaises(ProviderPending):
            observe_capabilities(self.paths, "claude-code")
        with self.assertRaises(FileNotFoundError):
            self.store.capabilities("claude-code")

    def test_legacy_single_file_still_reads(self):
        legacy = self.paths.vibe / "provider-actions" / "capabilities.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({
            "schema_version": 1,
            "adapter_id": "claude-code",
            "facts": CLAUDE_FACTS,
            "provenance": "recorded by an older vibe",
        }), encoding="utf-8")
        self.assertEqual(self.store.capabilities()["adapter_id"], "claude-code")
        self.assertEqual(
            self.store.capabilities("claude-code")["facts"], CLAUDE_FACTS
        )
        observed = observe_capabilities(self.paths)
        self.assertEqual(observed.adapter_id, "claude-code")

    def test_legacy_file_for_another_adapter_is_not_borrowed(self):
        legacy = self.paths.vibe / "provider-actions" / "capabilities.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({
            "schema_version": 1,
            "adapter_id": "workbuddy",
            "facts": WORKBUDDY_FACTS,
            "provenance": "recorded by an older vibe",
        }), encoding="utf-8")
        with self.assertRaises(FileNotFoundError):
            self.store.capabilities("claude-code")
        with self.assertRaises(ProviderPending):
            observe_capabilities(self.paths, "claude-code")


class AmbiguityTests(_Case):
    def test_adapterless_read_refuses_to_guess_among_several_records(self):
        self.init()
        self.attest("claude-code", CLAUDE_FACTS)
        self.attest("workbuddy", WORKBUDDY_FACTS)
        with self.assertRaises(ValueError):
            self.store.capabilities()
        with self.assertRaises(ProviderPending):
            observe_capabilities(self.paths)


if __name__ == "__main__":
    unittest.main()
