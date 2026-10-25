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

    def attest(self, adapter, facts, provenance="test session", project_id=None):
        facts_path = self.root / ("facts-{}.json".format(adapter))
        facts_path.write_text(json.dumps(facts), encoding="utf-8")
        argv = ["attest", "--json", "--adapter", adapter,
                "--facts", facts_path.name, "--provenance", provenance]
        if project_id is not None:
            argv += ["--project-id", project_id]
        return run_cli(argv, self.root)


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


SPEC_FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
COMPLEX_REQUEST = "设计并实现支付系统，迁移数据、集成接口、编写测试并部署"


class MultiAdapterPublishTests(_Case):
    """A complex ``--from-prd`` publish must name its adapter among several.

    The product spec cannot carry adapter_id (engineering field), so the
    calling session declares itself with ``vibe plan --adapter``; without it
    several attested adapters leave the publish unambiguously blocked.
    """

    def _plan(self, plan_id, *extra):
        shutil.copy(SPEC_FIXTURE, self.root / "product-spec.json")
        return run_cli(
            ["plan", "--json", "--request", COMPLEX_REQUEST, "--plan-id", plan_id,
             "--s1", "5,5,5,5,5", "--from-prd", "product-spec.json", *extra],
            self.root,
        )

    def _attest_both(self):
        self.attest("claude-code", CLAUDE_FACTS, project_id="pmpathprobe")
        self.attest("workbuddy", WORKBUDDY_FACTS, project_id="pmpathprobe")

    def test_adapterless_publish_is_blocked_with_an_adapter_hint(self):
        self.init()
        self._attest_both()
        result = self._plan("pm-plan")
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        reason = result.payload.get("reason", "")
        self.assertIn("engine_attestation_unavailable", reason)
        self.assertIn("--adapter", reason)
        self.assertIn("claude-code", reason)
        self.assertIn("workbuddy", reason)

    def test_publish_with_adapter_binds_that_adapters_record(self):
        self.init()
        self._attest_both()
        result = self._plan("pm-plan", "--adapter", "claude-code")
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        plan_dir = self.root / ".vibe" / "plans" / "pm-plan"
        nodes = json.loads((plan_dir / "nodes.json").read_text(encoding="utf-8"))
        business = [node for node in nodes if node["id"] != "integration-review"]
        self.assertTrue(business)
        for node in business:
            self.assertEqual(node["contract"]["adapter_id"], "claude-code", node["id"])
        attestation = json.loads(
            (plan_dir / "engine-attestation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(attestation["provider"], "claude-code")

    def test_publish_with_the_other_adapter_uses_its_record(self):
        self.init()
        self._attest_both()
        result = self._plan("pm-plan", "--adapter", "workbuddy")
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        attestation = json.loads(
            (self.root / ".vibe" / "plans" / "pm-plan" / "engine-attestation.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(attestation["provider"], "workbuddy")

    def test_unregistered_adapter_never_borrows_anothers_record(self):
        self.init()
        self._attest_both()
        result = self._plan("pm-plan", "--adapter", "cursor")
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        self.assertIn("engine_attestation_unavailable", result.payload.get("reason", ""))
        self.assertIn("cursor", result.payload.get("reason", ""))
        self.assertFalse((self.root / ".vibe" / "plans" / "pm-plan").exists())


class SymlinkPolicyTests(_Case):
    def test_symlinked_record_reads_as_unregistered_like_capabilities_all(self):
        self.init()
        self.attest("workbuddy", WORKBUDDY_FACTS)
        store_dir = self.paths.vibe / "provider-actions"
        (store_dir / "real-claude.json").write_text(json.dumps({
            "schema_version": 1,
            "adapter_id": "claude-code",
            "facts": CLAUDE_FACTS,
            "provenance": "planted outside the bridge",
        }), encoding="utf-8")
        (store_dir / "capabilities-claude-code.json").symlink_to("real-claude.json")
        with self.assertRaises(FileNotFoundError):
            self.store.capabilities("claude-code")
        self.assertNotIn("claude-code", self.store.capabilities_all())


if __name__ == "__main__":
    unittest.main()
