import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from tests.test_stage_handoff import _node_spec, _run_plan


class PlanAuthorizeSlimOutputTests(unittest.TestCase):
    def test_plan_stdout_is_slim_and_card_is_recoverable_from_path(self):
        temporary, root, result = _run_plan(_node_spec())
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.exit_code, 0, result.payload)
        payload = result.payload
        text = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["nodes"], ["node"])
        card = payload["authorization_card"]
        self.assertEqual(card["status"], "ok")
        self.assertNotIn("contract", payload["plan"])
        self.assertNotIn("file_scope", card)
        self.assertNotIn("contract_test_commands", card)
        raw = Path(card["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), card["sha256"])
        full = json.loads(raw)
        self.assertEqual(full["digest"], payload["authorization_digest"])
        self.assertGreater(len(json.dumps(full)), 0)
        self.assertIn("score", payload)
        self.assertLess(len(text), len(json.dumps(full)) + 4000)

    def test_unreadable_card_reports_unknown(self):
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.supervisor import authorization_card_summary
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".vibe/plans/p").mkdir(parents=True)
            summary = authorization_card_summary(ProjectPaths(root), "p")
            self.assertEqual(summary["status"], "unknown")


class SupervisorMailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.requests = self.root / ".vibe/provider-actions/requests"
        self.requests.mkdir(parents=True)

    def write(self, name, prompt):
        action = {
            "schema_version": 1, "action_id": name, "operation": "create_thread", "provider": "codex",
            "run_id": "run-1", "issue_id": "n1", "role": "developer", "generation": 1, "sequence": 0,
            "native_tool": "create_thread", "request_digest": "d" * 64,
            "request": {"prompt": prompt, "child_binding": {"node_id": "n1", "role": "developer"}},
        }
        path = self.requests / (name + ".json")
        path.write_text(json.dumps(action, ensure_ascii=False), encoding="utf-8")
        return path

    def test_prompt_body_replaced_by_digest_and_matches_file(self):
        paths = [self.write("action-a", "派发正文A" * 500), self.write("action-b", "body-B" * 500)]
        result = run_cli(["supervisor-mailbox", "--run-id", "run-1", "--json"], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        text = json.dumps(result.payload, ensure_ascii=False)
        self.assertNotIn("派发正文A", text)
        self.assertNotIn("body-B", text)
        self.assertEqual(result.payload["pending_count"], 2)
        for item in result.payload["pending"]:
            self.assertEqual(item["operation"], "create_thread")
            self.assertEqual(item["native_tool"], "create_thread")
            self.assertEqual(item["issue_id"], "n1")
            self.assertEqual(item["role"], "developer")
            self.assertEqual(item["child_binding"]["node_id"], "n1")
            ref = item["prompt"]
            stored = json.loads(Path(ref["path"]).read_text(encoding="utf-8"))
            body = stored["request"]["prompt"].encode("utf-8")
            self.assertEqual(ref["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(ref["bytes"], len(body))
        self.assertEqual({Path(i["prompt"]["path"]) for i in result.payload["pending"]},
                         {p.resolve() for p in paths})

    def test_unreadable_request_reports_unknown(self):
        (self.requests / "action-bad.json").write_text("not json", encoding="utf-8")
        result = run_cli(["supervisor-mailbox", "--run-id", "run-1", "--json"], self.root)
        self.assertEqual(result.payload["status"], "unknown")
        self.assertNotEqual(result.exit_code, 0)

    def test_run_id_required(self):
        result = run_cli(["supervisor-mailbox", "--json"], self.root)
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
