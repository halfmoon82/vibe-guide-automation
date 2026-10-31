"""IR-1/IR-2 (integration review): decision-reuse seams anchored across modules.

Anchors the cross-module behavior contracts named in the #338 contract, not
whole-file asserts:

- the scope/fingerprint ledger is written consistently through all three
  entries -- CLI-level raise (``raise_reusable_decision`` /
  ``ScopedDecisionMailbox.raise_decision``), the worker-delivery ingest entry
  (``ingest_worker_decisions``, node scope), and the monitor blocked_design
  path (``Monitor._ensure_blocked_design_decision``, run scope); records
  written before scopes existed read as node scope (fail closed);
- the #316 end-to-end seam: g1 asks and gets an answer, g2 raises the same
  question across nodes and reuses it (no new record, g1's decision_id and
  choice come back), and the answered summary reaches all three dispatch
  prompt paths -- generic create, visible-sdd create, and continuation --
  through the ``worker_questions.supervisor_answer_section`` seam;
- the options guard holds identically at CLI and library level;
- error behaviors: an empty question fingerprints to None and never blocks a
  raise, and an unreadable ledger reports ``unknown`` instead of faking an
  answer.

No network, no real desktop apps.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_auto_scope_files as fx

from vibe_guide import worker_questions
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.cli import SUCCESS, run_cli
from vibe_guide.decision_boundary import (
    ScopedDecisionMailbox,
    answer_decision,
    decision_fingerprint,
    find_reusable_decision,
    ingest_worker_decisions,
    raise_reusable_decision,
)
from vibe_guide.monitor import _BLOCKED_DESIGN_DECISIONS
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_snapshot, save_snapshot

_QUESTION = "该项目要不要提交评估？"
_SECTION_HEADER = "本 run 已答决策（scope=run）"
_NO_REPEAT_LINE = "同一问题不要重复提问"
_UNKNOWN_LINE = "本 run 已答决策台账不可读（unknown），已答决策未注入"
_SDD_PROTOCOL = "vibe_guide/protocols/visible-sdd-worker.md"


class LedgerWriteEntriesSeamTests(unittest.TestCase):
    """Seam 1: every write entry stamps scope+fingerprint; legacy fails closed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, {"VIBE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.paths = ProjectPaths.from_cwd(self.root)

    def _request_files(self):
        directory = self.root / ".vibe" / "decisions" / "requests"
        return list(directory.glob("*.json")) if directory.exists() else []

    def test_cli_level_raise_entry_stamps_run_scope_and_fingerprint(self):
        record = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])["request"]
        self.assertEqual(record["scope"], "run")
        self.assertEqual(record["fingerprint"], decision_fingerprint(_QUESTION))
        direct = ScopedDecisionMailbox(self.paths).raise_decision(
            run_id="run-1", node_id="g1", question="另一个问题？", options=["X", "Y"])
        self.assertEqual(direct["scope"], "run")
        self.assertEqual(direct["fingerprint"], decision_fingerprint("另一个问题？"))

    def test_worker_ingest_entry_stamps_node_scope_and_fingerprint(self):
        records, _ = ingest_worker_decisions(self.paths, "run-1", "g1", [{
            "question": _QUESTION,
            "options": ["要", "不要"],
        }])
        self.assertEqual(records[0]["scope"], "node")
        self.assertEqual(records[0]["fingerprint"], decision_fingerprint(_QUESTION))

    def test_legacy_record_without_scope_reads_as_node(self):
        record = {
            "schema_version": 1,
            "decision_id": "dec-irlegacy",
            "run_id": "run-1",
            "node_id": "g1",
            "question": "旧问题？",
            "category": "product_acceptance",
            "options": [{"id": "A", "label": "A", "impact": ""},
                        {"id": "B", "label": "B", "impact": ""}],
            "recommendation": None,
            "evidence": {},
            "asked_at": 1.0,
        }
        mailbox = ScopedDecisionMailbox(self.paths)
        record["request_digest"] = mailbox._request_digest(record)
        request_path = self.root / ".vibe" / "decisions" / "requests" / "dec-irlegacy.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(record), encoding="utf-8")
        result_path = self.root / ".vibe" / "decisions" / "results" / "dec-irlegacy.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({
            "schema_version": 1,
            "decision_id": "dec-irlegacy",
            "request_digest": record["request_digest"],
            "choice": "A",
            "answered_at": 1.0,
            "answered_via": "ir-test",
        }), encoding="utf-8")
        entries = mailbox.list_answered_scoped("run-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["scope"], "node")

    def test_empty_question_fingerprints_none_and_skips_the_ledger(self):
        for bad in ("", "   \n", None, 42):
            self.assertIsNone(decision_fingerprint(bad))
        # Even with an unreadable ledger the lookup is skipped, so the raise
        # path is never blocked by a None fingerprint.
        results = self.root / ".vibe" / "decisions" / "results"
        results.mkdir(parents=True)
        os.rmdir(results)
        os.symlink(str(self.home), str(results))
        matched, status = find_reusable_decision(self.paths, "run-1", "g2", "", ["A", "B"])
        self.assertIsNone(matched)
        self.assertEqual(status, "ok")

    def test_unreadable_ledger_reports_unknown_and_still_raises(self):
        results = self.root / ".vibe" / "decisions" / "results"
        results.mkdir(parents=True)
        os.rmdir(results)
        os.symlink(str(self.home), str(results))
        payload = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        self.assertFalse(payload["reused"])
        self.assertEqual(payload["reuse_status"], "unknown")
        self.assertEqual(len(self._request_files()), 1)
        contract = {"run_id": "run-1", "node_id": "g2", "role": "reviewer",
                    "generation": 1, "project_root": str(self.root)}
        self.assertEqual(worker_questions.supervisor_answer_section(contract), _UNKNOWN_LINE)


class ReuseAcrossNodesPromptSeamTests(unittest.TestCase):
    """Seam 2 (#316): g1 answered -> g2 reuses; all three prompt paths carry it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, {"VIBE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.paths = ProjectPaths.from_cwd(self.root)
        first = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        answer_decision(self.paths, first["request"]["decision_id"], "要",
                        request_digest=first["request"]["request_digest"])
        self.decision_id = first["request"]["decision_id"]

    def _contract(self, **extra):
        contract = {
            "run_id": "run-1", "node_id": "g2", "role": "reviewer", "generation": 1,
            "project_id": "p", "topology": "dual-visible",
            "worktree": "w", "branch": "b", "files": [],
            "project_root": str(self.root),
        }
        contract.update(extra)
        return contract

    def _create_prompt(self, contract):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        runner = ProviderActionRunner(
            ProjectPaths(Path(tempfile.mkdtemp())), "codex", "codex-app-visible")
        seen = []

        def fake(c, r, operation, request):
            seen.append((operation, request))
            return {"create": {"binding": {"task_id": "t", "host": "mac"}},
                    "locate": {"located": True},
                    "visibility": {"visible": True, "direct_enter": True}}[operation]

        runner._require_result = fake
        runner.task_binding(contract, Path(tempfile.mkdtemp()), "run-1", "running")
        return dict(seen)["create"]["prompt"]

    def _assert_summary(self, prompt):
        self.assertIn(_SECTION_HEADER, prompt)
        self.assertIn(self.decision_id, prompt)
        self.assertIn(_QUESTION, prompt)
        self.assertIn("结论：要", prompt)
        self.assertIn("来源：节点 g1", prompt)
        self.assertIn(_NO_REPEAT_LINE, prompt)

    def test_g2_reuses_g1_without_a_new_record(self):
        second = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g2",
            question=" {} ".format(_QUESTION), options=["要", "不要"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["request"]["decision_id"], self.decision_id)
        self.assertEqual(second["choice"], "要")
        directory = self.root / ".vibe" / "decisions" / "requests"
        self.assertEqual(len(list(directory.glob("*.json"))), 1)

    def test_generic_create_prompt_carries_the_answered_summary(self):
        self._assert_summary(self._create_prompt(self._contract(role="reviewer")))

    def test_visible_sdd_create_prompt_carries_the_answered_summary(self):
        prompt = self._create_prompt(self._contract(
            role="developer", topology="visible-sdd", sdd_protocol=_SDD_PROTOCOL))
        self._assert_summary(prompt)
        # The protocol block still follows the injected summary.
        self.assertLess(prompt.index(_SECTION_HEADER), prompt.index("## 会话必须遵循的协议"))

    def test_continuation_prompt_carries_the_answered_summary(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        prompt = ProviderActionRunner._continuation_prompt(self._contract(), self.root)
        self._assert_summary(prompt)

    def test_prompt_builder_injects_the_answer_section_verbatim(self):
        contract = self._contract()
        expected = worker_questions.supervisor_answer_section(contract)
        self.assertTrue(expected)
        prompt = self._create_prompt(contract)
        self.assertIn(expected, prompt)

    def test_options_guard_holds_at_library_and_cli_level(self):
        # Library: same question, old choice not among the new options -> new.
        rejected = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g2",
            question=_QUESTION, options=["必须", "可选"])
        self.assertFalse(rejected["reused"])
        directory = self.root / ".vibe" / "decisions" / "requests"
        self.assertEqual(len(list(directory.glob("*.json"))), 2)
        # CLI: identical behavior for the same seam.
        first = run_cli([
            "decision-raise", "--json", "--run-id", "run-2", "--node", "g1",
            "--question", _QUESTION, "--options", '["要", "不要"]',
        ], cwd=self.root)
        self.assertEqual(first.exit_code, SUCCESS, first.payload)
        answer_decision(
            self.paths, first.payload["request"]["decision_id"], "要",
            request_digest=first.payload["request"]["request_digest"])
        second = run_cli([
            "decision-raise", "--json", "--run-id", "run-2", "--node", "g2",
            "--question", _QUESTION, "--options", '["必须", "可选"]',
        ], cwd=self.root)
        self.assertEqual(second.exit_code, SUCCESS, second.payload)
        self.assertFalse(second.payload["reused"])
        cli_dir = self.root / ".vibe" / "decisions" / "requests"
        run_two = [p for p in cli_dir.glob("*.json")
                   if json.loads(p.read_text()).get("run_id") == "run-2"]
        self.assertEqual(len(run_two), 2)


class MonitorBlockedDesignScopeSeamTests(unittest.TestCase):
    """Seam 3: the monitor's blocked_design write is run-scoped ledger data."""

    setUp = fx.MonitorAutoScopeTests.setUp
    tearDown = fx.MonitorAutoScopeTests.tearDown

    def _blocked_design_run(self, cause="integration_scope_review"):
        node = fx._node("n1")
        plan = fx.Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        card = build_authorization_card(plan, [node], self.capabilities)
        monitor = fx.Monitor(self.paths, plan, [node])
        runner = FakeRunner()
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        current = snapshot.nodes["n1"]
        current.update({"status": "blocked_design", "reason": "design decision required",
                        "active_role": None, "active_task": None, "retryable_action": None,
                        "blocked_design_cause": cause})
        snapshot.handles.pop("n1", None)
        save_snapshot(self.paths, snapshot)
        return monitor, snapshot

    def test_blocked_design_record_carries_run_scope_and_fingerprint(self):
        monitor, snapshot = self._blocked_design_run()
        live = load_snapshot(self.paths, snapshot.run_id)
        self.assertTrue(monitor._ensure_blocked_design_decision(live, "n1"))
        pending = ScopedDecisionMailbox(self.paths).list_pending(snapshot.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["scope"], "run")
        self.assertEqual(pending[0]["fingerprint"],
                         decision_fingerprint(pending[0]["question"]))

    def test_blocked_design_answer_feeds_run_level_reuse(self):
        monitor, snapshot = self._blocked_design_run()
        live = load_snapshot(self.paths, snapshot.run_id)
        self.assertTrue(monitor._ensure_blocked_design_decision(live, "n1"))
        record = ScopedDecisionMailbox(self.paths).list_pending(snapshot.run_id)[0]
        answer_decision(self.paths, record["decision_id"], "keep_scope",
                        request_digest=record["request_digest"])
        spec = _BLOCKED_DESIGN_DECISIONS["integration_scope_review"]
        reused = raise_reusable_decision(
            self.paths, run_id=snapshot.run_id, node_id="integration-reviewer",
            question=spec["question"], options=[dict(item) for item in spec["options"]])
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["request"]["decision_id"], record["decision_id"])
        self.assertEqual(reused["choice"], "keep_scope")


if __name__ == "__main__":
    unittest.main()
