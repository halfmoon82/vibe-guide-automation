"""Phase 2 contract: `vibe attest` records session-observed capability facts.

vibe never probes or infers a host platform's capabilities. The session that
actually sees the native tools states each manifest fact as true/false, and
attest validates the shape, writes the bridge file, and reports what the
adapter detection makes of it.  Writer == reader is asserted against
ProviderActionStore, the only consumer.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths

FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
COMPLEX_REQUEST = "设计并实现支付系统，迁移数据、集成接口、编写测试并部署"
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


class _Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v45-attest-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)

    def init(self):
        result = run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(result.payload.get("status"), "ok", result.payload)

    def facts_file(self, facts, name="facts.json"):
        (self.root / name).write_text(json.dumps(facts), encoding="utf-8")
        return name

    def attest(self, facts=CLAUDE_FACTS, adapter="claude-code", provenance="session tool inventory 2026-09-17", project_id=None):
        argv = ["attest", "--json", "--adapter", adapter, "--facts", self.facts_file(facts), "--provenance", provenance]
        if project_id is not None:
            argv += ["--project-id", project_id]
        return run_cli(argv, self.root)


class AttestWritesTheBridgeTests(_Case):
    def test_attest_writes_capabilities_readable_by_provider_action_store(self):
        self.init()
        result = self.attest(project_id="pmpathprobe")
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        observed = ProviderActionStore(self.paths).capabilities()
        self.assertEqual(observed["adapter_id"], "claude-code")
        self.assertEqual(observed["facts"], CLAUDE_FACTS)
        self.assertEqual(observed["provenance"], "session tool inventory 2026-09-17")
        self.assertEqual(observed["project_id"], "pmpathprobe")
        self.assertEqual(result.payload["capabilities_path"], ".vibe/provider-actions/capabilities.json")

    def test_attest_reports_detection_level_and_visibility(self):
        self.init()
        full = self.attest()
        self.assertEqual(full.payload["level"], "full")
        self.assertTrue(full.payload["visible_automation"])
        guide = self.attest(facts={**CLAUDE_FACTS, "claude-code.subprocess": False})
        self.assertEqual(guide.payload["level"], "guide")
        self.assertFalse(guide.payload["visible_automation"])

    def test_attest_overwrites_the_previous_record(self):
        self.init()
        self.attest(project_id="first")
        self.attest(project_id="second")
        self.assertEqual(ProviderActionStore(self.paths).capabilities()["project_id"], "second")
        self.attest()
        self.assertNotIn("project_id", ProviderActionStore(self.paths).capabilities())

    def test_attest_then_from_prd_publishes_a_complex_plan(self):
        self.init()
        self.attest(project_id="pmpathprobe")
        shutil.copy(FIXTURE, self.root / "product-spec.json")
        planned = run_cli(
            ["plan", "--json", "--request", COMPLEX_REQUEST, "--plan-id", "pm-plan", "--from-prd", "product-spec.json"],
            self.root,
        )
        self.assertEqual(planned.payload.get("status"), "ok", planned.payload)
        self.assertTrue((self.root / ".vibe" / "plans" / "pm-plan" / "engine-attestation.json").is_file())


class AttestValidationTests(_Case):
    def test_attest_rejects_unknown_fact_names(self):
        self.init()
        result = self.attest(facts={**CLAUDE_FACTS, "claude-code.telepathy": True})
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("claude-code.telepathy", result.payload.get("reason", ""))
        self.assertFalse((self.root / ".vibe" / "provider-actions" / "capabilities.json").exists())

    def test_attest_rejects_missing_fact_names(self):
        self.init()
        facts = dict(CLAUDE_FACTS)
        facts.pop("claude-code.visible_task.wait")
        result = self.attest(facts=facts)
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("claude-code.visible_task.wait", result.payload.get("reason", ""))
        self.assertFalse((self.root / ".vibe" / "provider-actions" / "capabilities.json").exists())

    def test_attest_rejects_non_bool_facts(self):
        self.init()
        result = self.attest(facts={**CLAUDE_FACTS, "claude-code.shell": "true"})
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("claude-code.shell", result.payload.get("reason", ""))

    def test_attest_rejects_unknown_adapter(self):
        self.init()
        result = self.attest(adapter="nonexistent-agent", facts={"nonexistent-agent.shell": True})
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("nonexistent-agent", result.payload.get("reason", ""))

    def test_attest_requires_provenance_and_all_three_arguments(self):
        self.init()
        missing = run_cli(["attest", "--json", "--adapter", "claude-code"], self.root)
        self.assertNotEqual(missing.payload.get("status"), "ok")
        self.assertIn("--facts", missing.payload.get("reason", ""))
        empty = self.attest(provenance="   ")
        self.assertNotEqual(empty.payload.get("status"), "ok")
        self.assertIn("provenance", empty.payload.get("reason", ""))

    def test_attest_requires_an_initialized_project(self):
        result = self.attest()
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("init", result.payload.get("reason", ""))
        self.assertFalse((self.root / ".vibe").exists())


if __name__ == "__main__":
    unittest.main()
