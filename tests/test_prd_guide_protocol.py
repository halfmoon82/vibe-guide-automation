"""Phase 3 contract: the PRD-guide protocol is shipped, materialized, and enforced.

The protocol is what the host agent reads to run the PRD conversation; vibe
owns validation.  These tests pin (1) the protocol's embedded schema to
node_spec.PRODUCT_SPEC_FIELDS so the document cannot drift from the code,
(2) init materializing it into the project, (3) unresolved
`needs_confirmation` items blocking publish, and (4) goal traceability rows
landing in planning-brief.md.
"""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
COMPLEX_REQUEST = "设计并实现支付系统，迁移数据、集成接口、编写测试并部署"
CAPABILITIES = {
    "schema_version": 1,
    "adapter_id": "claude-code",
    "facts": {name: True for name in (
        "claude-code.agent", "claude-code.shell", "claude-code.subprocess", "claude-code.worktree",
        "claude-code.visible_task.create", "claude-code.visible_task.enter",
        "claude-code.visible_task.resume", "claude-code.visible_task.wait",
    )},
    "provenance": "test fixture",
    "project_id": "pmpathprobe",
}


def _product_spec():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ProtocolShippingTests(unittest.TestCase):
    def test_protocol_schema_example_matches_node_spec_schema(self):
        from vibe_guide.node_spec import PRODUCT_SPEC_FIELDS
        from vibe_guide.protocols import load_protocol, protocol_schema_example
        text = load_protocol("prd-guide")
        self.assertEqual(protocol_schema_example(text), PRODUCT_SPEC_FIELDS)

    def test_protocol_lists_every_engineering_field_as_forbidden(self):
        from vibe_guide.node_spec import ENGINEERING_FIELDS
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        for group in ENGINEERING_FIELDS.values():
            for name in group:
                self.assertIn("`{}`".format(name), text, name)

    def test_protocol_is_shipped_as_package_data(self):
        from vibe_guide import protocols
        self.assertTrue((Path(protocols.__file__).parent / "prd-guide.md").is_file())
        setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn("protocols/*.md", setup_text)

    def test_protocol_mentions_every_cli_command_it_relies_on(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        for command in ("vibe attest", "--from-prd", "vibe authorize", "vibe monitor", "vibe status", "vibe resume"):
            self.assertIn(command, text, command)
        for marker in ("user_confirmed", "system_inferred", "needs_confirmation", "unverified"):
            self.assertIn(marker, text, marker)


class _ProjectCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v45-prd-guide-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)

    def init(self):
        result = run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        return result

    def write_capabilities(self):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(json.dumps(CAPABILITIES), encoding="utf-8")

    def plan_from_prd(self, spec, plan_id="pm-plan"):
        (self.root / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return run_cli(
            ["plan", "--json", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--from-prd", "product-spec.json"],
            self.root,
        )


class InitMaterializationTests(_ProjectCase):
    def test_init_materializes_prd_guide_proposal(self):
        from vibe_guide.protocols import PRD_GUIDE_PROPOSAL_RELATIVE, load_protocol
        first = self.init()
        skill = self.root / PRD_GUIDE_PROPOSAL_RELATIVE
        self.assertTrue(skill.is_file(), PRD_GUIDE_PROPOSAL_RELATIVE)
        self.assertEqual(skill.read_text(encoding="utf-8"), load_protocol("prd-guide"))
        self.assertIn(PRD_GUIDE_PROPOSAL_RELATIVE, first.payload.get("created", []))
        agentsmd_proposal = (self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md").read_text(encoding="utf-8")
        self.assertIn(PRD_GUIDE_PROPOSAL_RELATIVE, agentsmd_proposal)
        self.assertIn("## Capability and Tool Truth", agentsmd_proposal)
        skill.write_text("user edited", encoding="utf-8")
        second = self.init()
        self.assertEqual(skill.read_text(encoding="utf-8"), "user edited")
        self.assertNotIn(PRD_GUIDE_PROPOSAL_RELATIVE, second.payload.get("created", []))


class ProtocolEnforcementTests(_ProjectCase):
    def test_needs_confirmation_blocks_publish(self):
        self.init()
        self.write_capabilities()
        spec = _product_spec()
        spec["prd"]["success_criteria"].append(
            {"value": "导出是否需要带公司水印尚未确认", "source": "needs_confirmation"}
        )
        result = self.plan_from_prd(spec)
        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertIn("水印", result.payload.get("question", ""))
        self.assertFalse((self.root / ".vibe" / "plans" / "pm-plan").exists())

    def test_planning_brief_lists_goal_traceability_rows(self):
        self.init()
        self.write_capabilities()
        spec = _product_spec()
        spec["goals"] = [{
            "id": "G1",
            "user_scenario": "客户经理导出所选日期范围的保单 PDF",
            "code_evidence": "src/pages/policy/view.tsx",
            "spec_ref": "specs/export-button.md",
            "issue_ref": "issues/export-button.md",
            "dag_nodes": ["export-button", "date-range-filter"],
            "runtime_acceptance": "导出的 PDF 字段与页面一致",
        }]
        result = self.plan_from_prd(spec)
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        brief = (self.root / ".vibe" / "plans" / "pm-plan" / "planning-brief.md").read_text(encoding="utf-8")
        self.assertRegex(brief, r"\|\s*G1\s*\|")
        self.assertIn("export-button", brief)

    def test_incomplete_goal_blocks_publish(self):
        self.init()
        self.write_capabilities()
        spec = _product_spec()
        spec["goals"] = [{"id": "G1", "user_scenario": "只写了场景"}]
        result = self.plan_from_prd(spec)
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("traceability", result.payload.get("reason", ""))
        self.assertFalse((self.root / ".vibe" / "plans" / "pm-plan").exists())


if __name__ == "__main__":
    unittest.main()
