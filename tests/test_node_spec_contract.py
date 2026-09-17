"""Phase 1 contract: a product-only spec publishes without hand-written engineering fields.

Every test here drives the real CLI in a temporary project (``vibe init`` +
``vibe plan``) so the artifacts under test are the ones a user would have.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import run_cli
from vibe_guide.diagnostics import assert_planning_gate
from vibe_guide.models import AgentCapabilities, DAGNode, IntegrationAcceptanceContract
from vibe_guide.paths import ProjectPaths
from vibe_guide.session_entry import build_session_entry

from tests.support_v45_authorize import publish_complex_probe

FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
COMPLEX_REQUEST = "设计并实现支付系统，迁移数据、集成接口、编写测试并部署"

CAPABILITIES = {
    "schema_version": 1,
    "adapter_id": "claude-code",
    "facts": {
        "claude-code.agent": True,
        "claude-code.shell": True,
        "claude-code.subprocess": True,
        "claude-code.worktree": True,
        "claude-code.visible_task.create": True,
        "claude-code.visible_task.enter": True,
        "claude-code.visible_task.resume": True,
        "claude-code.visible_task.wait": True,
    },
    "provenance": "test fixture: native visible-task tools observed in this session",
    "project_id": "pmpathprobe",
}


def _product_spec():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class _ProjectCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v45-node-spec-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)
        init = run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(init.payload.get("status"), "ok", init.payload)

    def write_capabilities(self, **overrides):
        payload = dict(CAPABILITIES)
        payload.update(overrides)
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def write_spec(self, spec, name="product-spec.json"):
        (self.root / name).write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return name

    def plan_from_prd(self, spec_name, plan_id="pm-plan", request=COMPLEX_REQUEST):
        return run_cli(
            ["plan", "--json", "--request", request, "--plan-id", plan_id, "--from-prd", spec_name],
            self.root,
        )

    def plan_dir(self, plan_id="pm-plan"):
        return self.root / ".vibe" / "plans" / plan_id


class ProductSpecFieldBoundaryTests(unittest.TestCase):
    def test_fixture_contains_no_engineering_fields(self):
        from vibe_guide.node_spec import reject_engineering_fields
        reject_engineering_fields(_product_spec())
        polluted = _product_spec()
        polluted["project_id"] = "hand-written"
        with self.assertRaises(ValueError) as caught:
            reject_engineering_fields(polluted)
        self.assertIn("project_id", str(caught.exception))
        node_polluted = _product_spec()
        node_polluted["nodes"][0]["contract"]["adapter_id"] = "codex"
        with self.assertRaises(ValueError) as caught:
            reject_engineering_fields(node_polluted)
        self.assertIn("adapter_id", str(caught.exception))


class ProductSpecPublishTests(_ProjectCase):
    def test_entry_route_band_equals_published_plan_band(self):
        self.write_capabilities()
        result = self.plan_from_prd(self.write_spec(_product_spec()))
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(result.payload.get("route"), "complex")
        plan = json.loads((self.plan_dir() / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["complexity_band"], "complex")
        self.assertTrue((self.plan_dir() / "engine-attestation.json").is_file())
        self.assertIn("integration-review", plan["node_ids"])

    def test_product_only_spec_publishes_without_hand_written_engineering_fields(self):
        self.write_capabilities()
        result = self.plan_from_prd(self.write_spec(_product_spec()))
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        nodes = json.loads((self.plan_dir() / "nodes.json").read_text(encoding="utf-8"))
        business = [node for node in nodes if node["id"] != "integration-review"]
        self.assertEqual({node["id"] for node in business}, {"export-button", "date-range-filter"})
        for node in business:
            self.assertEqual(node["contract"]["adapter_id"], "claude-code", node["id"])
            self.assertEqual(node["contract"]["project_id"], "pmpathprobe", node["id"])
            self.assertEqual(node["status"], "planned", node["id"])
            self.assertEqual(node["parallel_group"], "export-ui", node["id"])
        plan = json.loads((self.plan_dir() / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(plan["integration_contract"]) & {
                "iteration_context", "compatibility_scope", "agentsmd_acceptance_refs",
                "integration_acceptance_contract", "unverified_or_excluded",
            },
            {
                "iteration_context", "compatibility_scope", "agentsmd_acceptance_refs",
                "integration_acceptance_contract", "unverified_or_excluded",
            },
        )
        gate = assert_planning_gate(self.paths, "pm-plan")
        self.assertEqual(gate.status, "execution_ready", gate)
        prd = (self.plan_dir() / "prd.md").read_text(encoding="utf-8")
        self.assertIn("状态：approved", prd)
        self.assertIn("user_confirmed", prd)
        brief = self.plan_dir() / "planning-brief.md"
        self.assertTrue(brief.is_file())
        self.assertEqual(plan["implementation_plan_path"], ".vibe/plans/pm-plan/planning-brief.md")

    def test_decisions_are_never_auto_approved(self):
        self.write_capabilities()
        spec = _product_spec()
        spec["decisions"][0]["status"] = "unresolved"
        spec["decisions"][0]["selected"] = None
        result = self.plan_from_prd(self.write_spec(spec))
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("product decisions remain unresolved", result.payload.get("reason", ""))
        self.assertFalse(self.plan_dir().exists())

    def test_engineering_fields_in_product_spec_are_rejected_before_publish(self):
        self.write_capabilities()
        spec = _product_spec()
        spec["project_id"] = "smuggled"
        result = self.plan_from_prd(self.write_spec(spec))
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("project_id", result.payload.get("reason", ""))
        self.assertFalse(self.plan_dir().exists())

    def test_empty_node_list_gives_a_readable_reason(self):
        self.write_capabilities()
        spec = _product_spec()
        spec["nodes"] = []
        result = self.plan_from_prd(self.write_spec(spec))
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("at least one node", result.payload.get("reason", ""))

    def test_missing_capabilities_blocks_complex_publish_with_existing_message(self):
        result = self.plan_from_prd(self.write_spec(_product_spec()))
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("engine_attestation_unavailable", result.payload.get("reason", ""))

    def test_legacy_node_spec_path_still_publishes(self):
        root = publish_complex_probe(self)
        plan = json.loads((root / ".vibe" / "plans" / "probe-plan" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "confirmed_pending_authorization")


class DraftAndRoutingTests(_ProjectCase):
    def test_draft_plan_is_reported_as_draft_not_json_error(self):
        drafted = run_cli(["plan", "--json", "--request", COMPLEX_REQUEST], self.root)
        self.assertEqual(drafted.payload.get("status"), "planned", drafted.payload)
        plan_id = drafted.payload["plan_id"]
        draft = json.loads((self.root / drafted.payload["materialized_path"] / "plan.json").read_text(encoding="utf-8"))
        self.assertIn("--from-prd", draft.get("next_step", ""))
        result = run_cli(["monitor", "--json", "--plan", plan_id, "--authorize", "AUTHORIZE"], self.root)
        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertIn("--from-prd", result.payload.get("reason", ""))
        self.assertNotIn("regular file", result.payload.get("reason", ""))

    def test_status_and_resume_report_a_draft_as_design_state(self):
        drafted = run_cli(["plan", "--json", "--request", COMPLEX_REQUEST], self.root)
        plan_id = drafted.payload["plan_id"]
        for command in ("status", "resume"):
            result = run_cli([command, "--json", "--plan", plan_id], self.root)
            self.assertEqual(result.payload.get("status"), "blocked_design", (command, result.payload))
            self.assertIn("--from-prd", result.payload.get("reason", ""), command)
            self.assertNotIn("authorization invalidated", result.payload.get("reason", ""), command)
            self.assertFalse((self.root / ".vibe" / "plans" / plan_id / "authorization-invalidated.json").exists(), command)

    def test_unknown_adapter_in_capabilities_is_reported_not_crashed(self):
        self.write_capabilities(adapter_id="nonexistent-agent", facts={"nonexistent-agent.shell": True})
        result = self.plan_from_prd(self.write_spec(_product_spec()))
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("nonexistent-agent", result.payload.get("reason", ""))

    def test_invalid_s1_flag_is_rejected_not_silently_defaulted(self):
        with self.assertRaises(ValueError) as caught:
            build_session_entry(COMPLEX_REQUEST, s1="garbage")
        self.assertIn("--s1", str(caught.exception))
        result = run_cli(["plan", "--json", "--request", COMPLEX_REQUEST, "--s1", "garbage"], self.root)
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("--s1", result.payload.get("reason", ""))
        self.assertEqual(build_session_entry(COMPLEX_REQUEST, s1=None).route.route, "complex")


class NormalizedSpecModelContractTests(_ProjectCase):
    def test_normalized_keys_are_accepted_by_models(self):
        from vibe_guide.node_spec import normalize_node_spec
        self.write_capabilities()
        entry = build_session_entry(COMPLEX_REQUEST, plan_id="pm-plan")
        normalized = normalize_node_spec(_product_spec(), entry, self.paths)
        self.assertEqual(normalized["complexity_band"], "complex")
        for node in normalized["nodes"]:
            DAGNode.from_dict(json.loads(json.dumps(node)))
        IntegrationAcceptanceContract.from_dict(normalized["integration_contract"])
        AgentCapabilities.from_dict(normalized["capabilities"])
        self.assertEqual(normalized["project_id"], "pmpathprobe")
        self.assertEqual(normalized["spec_path"], ".vibe/plans/pm-plan/prd.md")
        self.assertEqual(normalized["decisions"], _product_spec()["decisions"])

    def test_normalize_fills_missing_but_does_not_overwrite_present_fields(self):
        from vibe_guide.node_spec import normalize_node_spec
        self.write_capabilities()
        entry = build_session_entry(COMPLEX_REQUEST, plan_id="pm-plan")
        spec = _product_spec()
        spec["remote_git_actions"] = "allow"
        spec["nodes"][0]["parallel_group"] = ""
        normalized = normalize_node_spec(spec, entry, self.paths)
        self.assertEqual(normalized["remote_git_actions"], "allow")
        self.assertIsNone(normalized["nodes"][0]["parallel_group"])
        self.assertEqual(normalized["nodes"][1]["parallel_group"], "export-ui")


class BandGovernanceBoundaryTests(_ProjectCase):
    def test_route_governs_band_only_on_the_product_path(self):
        # --from-prd: the session route is authoritative (fail-closed).
        # --node-spec: the hand-written spec keeps declaring its own band, so a
        # legacy background spec is not silently promoted into the engine
        # attestation / integration-review gates it never opted into.
        from vibe_guide.node_spec import normalize_node_spec
        legacy = json.loads((Path(__file__).parent / "fixtures" / "e2e-project" / "plan-source.json").read_text(encoding="utf-8"))
        entry = build_session_entry(COMPLEX_REQUEST, s1="4,4,4,4,4", plan_id="legacy")
        self.assertEqual(entry.route.route, "complex")
        as_legacy = normalize_node_spec(legacy, entry, self.paths, route_governs_band=False)
        self.assertNotEqual(as_legacy.get("complexity_band"), "complex")
        self.assertNotIn("integration_contract", as_legacy)
        self.write_capabilities()
        as_product = normalize_node_spec(_product_spec(), entry, self.paths, route_governs_band=True)
        self.assertEqual(as_product["complexity_band"], "complex")
        self.assertIn("integration_contract", as_product)


class CapabilitiesSchemaTests(_ProjectCase):
    def test_capabilities_schema_accepts_optional_project_id_and_rejects_others(self):
        self.write_capabilities()
        store = ProviderActionStore(self.paths)
        observed = store.capabilities()
        self.assertEqual(observed["project_id"], "pmpathprobe")
        self.write_capabilities(unexpected="x")
        with self.assertRaises(ValueError):
            store.capabilities()
        store.publish_capabilities("claude-code", CAPABILITIES["facts"], "session", project_id="fromapi")
        self.assertEqual(store.capabilities()["project_id"], "fromapi")
        store.publish_capabilities("claude-code", CAPABILITIES["facts"], "session")
        self.assertNotIn("project_id", store.capabilities())


if __name__ == "__main__":
    unittest.main()
