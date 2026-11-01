"""WorkBuddy cannot open visible task windows; the host session serves the mailbox itself.

Regression for the 留学辅导 run (2026-10/11): the topology ruling was already
``visible-sdd`` (the attested ``workbuddy.in_session_sdd`` fact), but every
pending ``create`` still named ``workbuddy_job__create``.  A session without
that tool was told to report a tool-routing fault and stop, and the printed
result template carried Codex-only names (``threadId`` / ``hostId``).
"""

import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore, _canonical_digest
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import TEMPLATE_PLACEHOLDER, validate_result_shape
from vibe_guide.supervisor import mailbox_result_template

RUN = "run-1"
IN_SESSION_ONLY = {
    "workbuddy.agent": True, "workbuddy.shell": True, "workbuddy.subprocess": True,
    "workbuddy.worktree": True, "workbuddy.in_session_sdd": True,
    "workbuddy.visible_task.create": False, "workbuddy.visible_task.enter": False,
    "workbuddy.visible_task.resume": False, "workbuddy.visible_task.wait": False,
}
FULL_VISIBLE = dict(IN_SESSION_ONLY, **{
    "workbuddy.visible_task.create": True, "workbuddy.visible_task.enter": True,
    "workbuddy.visible_task.resume": True, "workbuddy.visible_task.wait": True,
})
NATIVE = {
    "workbuddy-visible": {"create": "workbuddy_job__create", "locate": "workbuddy_job__get",
                          "visibility": "workbuddy_job__list", "resume": "workbuddy_job__reply",
                          "wait": "workbuddy_job__wait"},
    "codex-app-visible": {"create": "codex_app__create_thread"},
}
PROVENANCE = "WorkBuddy 桌面会话实测：Agent 子代理可并行；无可见任务窗口工具"


def filled(example):
    return json.loads(json.dumps(example, ensure_ascii=False).replace(TEMPLATE_PLACEHOLDER, "real"))


class HostSessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)
        (self.root / ".vibe/provider-actions/requests").mkdir(parents=True)

    def attest(self, facts):
        ProviderActionStore(self.paths).publish_capabilities("workbuddy", facts, PROVENANCE)

    def request(self, name, operation="create", provider="workbuddy-visible",
                topology="visible-sdd", role="developer"):
        basis = {"operation": operation, "provider": provider, "run_id": RUN, "issue_id": "n1",
                 "role": role, "generation": 1, "sequence": 0}
        action_id = "action-" + _canonical_digest(basis)[:32]
        action = {"schema_version": 1, "action_id": action_id, **basis,
                  "native_tool": NATIVE[provider][operation],
                  "request": {"prompt": "go", "topology": topology}}
        action["request_digest"] = _canonical_digest(action)
        path = self.root / ".vibe/provider-actions/requests" / (action_id + ".json")
        path.write_text(json.dumps(action, ensure_ascii=False), encoding="utf-8")
        return action_id

    def pending(self):
        result = run_cli(["supervisor-mailbox", "--run-id", RUN, "--json"], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        return {item["action_id"]: item for item in result.payload["pending"]}

    def test_create_template_uses_the_provider_neutral_identity_fields(self):
        action_id = self.request("w")
        template = mailbox_result_template(self.paths, RUN, action_id)["template"]
        self.assertEqual(set(template["binding"]), {"task_id", "host"})
        validate_result_shape("create", filled(template), role="developer")

    def test_codex_create_template_keeps_its_own_names(self):
        action_id = self.request("c", provider="codex-app-visible", topology="dual-visible")
        template = mailbox_result_template(self.paths, RUN, action_id)["template"]
        self.assertEqual(set(template["binding"]), {"threadId", "hostId"})

    def test_in_session_only_host_is_told_to_serve_the_request_itself(self):
        self.attest(IN_SESSION_ONLY)
        ids = {op: self.request("w", operation=op) for op in ("create", "locate", "visibility")}
        pending = self.pending()
        for operation, action_id in ids.items():
            service = pending[action_id].get("service")
            self.assertIsInstance(service, dict, operation)
            self.assertEqual(service["mode"], "host_session")
            self.assertEqual(service["evidence_ref"], PROVENANCE)
        self.assertEqual(pending[ids["create"]]["service"]["binding_fields"], ["task_id", "host"])

    def test_full_visible_host_keeps_the_native_tool_path(self):
        self.attest(FULL_VISIBLE)
        action_id = self.request("w")
        self.assertNotIn("service", self.pending()[action_id])

    def test_unattested_host_never_gets_a_self_service_offer(self):
        action_id = self.request("w")
        self.assertNotIn("service", self.pending()[action_id])

    def test_the_offer_needs_the_attested_in_session_fact(self):
        self.attest(dict(IN_SESSION_ONLY, **{"workbuddy.in_session_sdd": False}))
        action_id = self.request("w")
        self.assertNotIn("service", self.pending()[action_id])

    def test_two_window_dispatch_is_never_self_served(self):
        self.attest(IN_SESSION_ONLY)
        action_id = self.request("w", topology="dual-visible", role="reviewer")
        self.assertNotIn("service", self.pending()[action_id])

    def test_other_providers_are_not_offered_self_service(self):
        self.attest(IN_SESSION_ONLY)
        action_id = self.request("c", provider="codex-app-visible")
        self.assertNotIn("service", self.pending()[action_id])


if __name__ == "__main__":
    unittest.main()


E2E_FIXTURE = Path(__file__).parent / "fixtures" / "e2e-project"
IN_SESSION_REVIEW = {
    "protocol": "vibe_guide/protocols/visible-sdd-worker.md",
    "evidence_ref": "session#1",
    "clearance": {"p0": 0, "p1": 0, "p2": 0},
    "coverage": {"mode": "none", "reason": "fixture has no file manifest"},
    "environment_facts_ref": "none",
}


class SelfServedRunTests(unittest.TestCase):
    """A WorkBuddy session with no ``workbuddy_job__*`` tools drives a run to acceptance.

    Only ``create`` carries the ruled topology; the later locate / visibility /
    wait requests do not, so the self-service offer has to follow the node's
    create request.  Nothing here uses the V4.4 proof: real dispatch never
    sets ``binding_contract_version``.
    """

    def setUp(self):
        import shutil

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        shutil.copytree(E2E_FIXTURE, self.root)
        self.cli = lambda argv: run_cli(argv + ["--json"], self.root)
        self.assertEqual(self.cli(["init", "--confirm"]).exit_code, 0)
        self.store = ProviderActionStore(ProjectPaths(self.root))
        self.store.publish_capabilities(
            "workbuddy", IN_SESSION_ONLY, PROVENANCE, project_id="project-fixture",
        )
        source = json.loads((self.root / "plan-source.json").read_text(encoding="utf-8"))
        source["capabilities"]["agent_id"] = "workbuddy"
        source["active_pair_limit"] = 1
        source["nodes"] = [source["nodes"][0]]
        contract = source["nodes"][0]["contract"]
        contract.update({"adapter_id": "workbuddy", "project_id": "project-fixture", "host": "mac"})
        for key in ("command", "provider", "mode"):
            contract.pop(key, None)
        (self.root / "wb-plan.json").write_text(json.dumps(source), encoding="utf-8")
        planned = self.cli([
            "plan", "--request", "设计并实现两个契约兼容的并行节点并完成独立审查",
            "--plan-id", "wb-plan", "--s1", "4,4,4,4,4", "--node-spec", "wb-plan.json",
        ])
        self.assertEqual(planned.exit_code, 0, planned.text)
        self.assertEqual(self.cli(["monitor", "--plan", "wb-plan", "--authorize", "AUTHORIZE"]).exit_code, 4)

    def test_session_self_answers_every_request_and_the_node_is_accepted(self):
        offered = {}
        result = None
        for _ in range(12):
            for action in self.store.pending():
                operation = action["operation"]
                view = {i["action_id"]: i for i in self.cli(
                    ["supervisor-mailbox", "--run-id", action["run_id"]]
                ).payload["pending"]}
                offered[operation] = view[action["action_id"]].get("service", {}).get("mode")
                if operation == "create":
                    payload = {"binding": {"task_id": "host-session", "host": "mac"}}
                elif operation == "locate":
                    payload = {"located": True}
                elif operation == "visibility":
                    payload = {"visible": True, "direct_enter": True}
                else:
                    payload = {"status": "completed", "event": "complete", "cursor": "c1",
                               "evidence": "dev+review done", "in_session_review": IN_SESSION_REVIEW}
                self.store.complete(action["action_id"], payload)
            result = self.cli(["resume", "--plan", "wb-plan"])
            if result.payload.get("status") == "complete":
                break
        self.assertEqual(result.payload.get("status"), "complete", result.text)
        self.assertEqual(
            offered,
            {op: "host_session" for op in ("create", "locate", "visibility", "wait")},
        )
        self.assertEqual(result.payload["nodes"]["api"]["status"], "accepted")
