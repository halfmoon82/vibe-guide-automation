"""Decision reuse across nodes within one run (#338).

Run-level answered decisions are injected into dispatch prompts and reused by
``decision-raise`` so a second node asking the same question does not create a
duplicate request.  Node-scoped decisions are never reused by other nodes.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide import worker_questions
from vibe_guide.cli import SUCCESS, run_cli
from vibe_guide.decision_boundary import (
    DECISION_SCOPES,
    ScopedDecisionMailbox,
    answer_decision,
    decision_fingerprint,
    ingest_worker_decisions,
    raise_reusable_decision,
)
from vibe_guide.paths import ProjectPaths

_QUESTION = "该项目要不要提交评估？"


class DecisionReuseTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, {"VIBE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.paths = ProjectPaths.from_cwd(self.root)
        self.mailbox = ScopedDecisionMailbox(self.paths)

    def _requests_files(self):
        directory = self.root / ".vibe" / "decisions" / "requests"
        return list(directory.glob("*.json")) if directory.exists() else []

    def _answer(self, record, choice="要"):
        answer_decision(
            self.paths,
            record["decision_id"],
            choice,
            request_digest=record["request_digest"],
        )

    # -- a. fingerprint normalization --------------------------------------
    def test_fingerprint_normalizes_whitespace(self):
        first = decision_fingerprint("  该项目  要不要\n提交评估？ ")
        second = decision_fingerprint("该项目 要不要 提交评估？")
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_fingerprint_none_for_empty_or_non_string(self):
        self.assertIsNone(decision_fingerprint(""))
        self.assertIsNone(decision_fingerprint("   \n\t "))
        self.assertIsNone(decision_fingerprint(None))
        self.assertIsNone(decision_fingerprint(123))

    def test_scopes_constant(self):
        self.assertEqual(DECISION_SCOPES, ("run", "node"))

    # -- b. scope + fingerprint on records ----------------------------------
    def test_new_record_carries_scope_and_fingerprint(self):
        record = self.mailbox.raise_decision(
            run_id="run-1", node_id="g1", question="Q?", options=["A", "B"])
        self.assertEqual(record["scope"], "run")
        self.assertEqual(record["fingerprint"], decision_fingerprint("Q?"))
        stored = json.loads(
            (self.root / ".vibe" / "decisions" / "requests" / (record["decision_id"] + ".json")).read_text())
        self.assertEqual(stored["scope"], "run")
        self.assertEqual(stored["fingerprint"], decision_fingerprint("Q?"))

    def test_legacy_record_without_scope_reads_as_node(self):
        record = {
            "schema_version": 1,
            "decision_id": "dec-legacyreuse",
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
        record["request_digest"] = self.mailbox._request_digest(record)
        request_path = self.root / ".vibe" / "decisions" / "requests" / "dec-legacyreuse.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(record), encoding="utf-8")
        result_path = self.root / ".vibe" / "decisions" / "results" / "dec-legacyreuse.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({
            "schema_version": 1,
            "decision_id": "dec-legacyreuse",
            "request_digest": record["request_digest"],
            "choice": "A",
            "answered_at": 1.0,
            "answered_via": "test",
        }), encoding="utf-8")
        entries = self.mailbox.list_answered_scoped("run-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["scope"], "node")
        self.assertEqual(entries[0]["choice"], "A")

    # -- c. acceptance main flow: g2 reuses g1's answered run decision -------
    def test_g2_reuses_g1_answered_run_decision(self):
        first = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        self.assertFalse(first["reused"])
        self.assertEqual(first["reuse_status"], "ok")
        self._answer(first["request"])
        second = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g2",
            question="  {} ".format(_QUESTION), options=["要", "不要"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["reuse_status"], "ok")
        self.assertEqual(second["request"]["decision_id"], first["request"]["decision_id"])
        self.assertEqual(second["choice"], "要")
        self.assertEqual(second["notification"]["status"], "reused")
        self.assertEqual(len(self._requests_files()), 1)

    # -- d. a different question still creates a second record --------------
    def test_different_question_creates_second_record(self):
        first = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        self._answer(first["request"])
        second = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g2",
            question="另一个问题？", options=["X", "Y"])
        self.assertFalse(second["reused"])
        self.assertNotEqual(
            second["request"]["decision_id"], first["request"]["decision_id"])
        self.assertEqual(len(self._requests_files()), 2)

    # -- e. node-scoped answers are never reused as run-level ---------------
    def test_node_scope_not_reused_across_nodes(self):
        records, _ = ingest_worker_decisions(self.paths, "run-1", "g1", [{
            "question": _QUESTION,
            "options": ["要", "不要"],
        }])
        self.assertEqual(records[0]["scope"], "node")
        self._answer(records[0])
        second = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g2",
            question=_QUESTION, options=["要", "不要"], scope="run")
        self.assertFalse(second["reused"])
        self.assertEqual(len(self._requests_files()), 2)

    def test_node_scope_reused_by_same_node_only(self):
        records, _ = ingest_worker_decisions(self.paths, "run-1", "g1", [{
            "question": _QUESTION,
            "options": ["要", "不要"],
        }])
        self._answer(records[0])
        same_node = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"], scope="node")
        self.assertTrue(same_node["reused"])
        other_node = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g2",
            question=_QUESTION, options=["要", "不要"], scope="node")
        self.assertFalse(other_node["reused"])

    # -- f. unreadable ledger: unknown status, still raises -----------------
    def test_unreadable_ledger_is_unknown_and_still_raises(self):
        results = self.root / ".vibe" / "decisions" / "results"
        results.mkdir(parents=True)
        os.rmdir(results)
        os.symlink(str(self.home), str(results))
        payload = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        self.assertFalse(payload["reused"])
        self.assertEqual(payload["reuse_status"], "unknown")
        self.assertEqual(len(self._requests_files()), 1)

    # -- g. supervisor_answer_section injection ----------------------------
    def _section(self, node_id="g2", role="reviewer"):
        return worker_questions.supervisor_answer_section({
            "run_id": "run-1",
            "node_id": node_id,
            "role": role,
            "generation": 1,
            "project_root": str(self.root),
        })

    def test_section_lists_run_scope_answered_decisions(self):
        first = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        self._answer(first["request"])
        section = self._section()
        self.assertIn(first["request"]["decision_id"], section)
        self.assertIn(_QUESTION, section)
        self.assertIn("结论：要", section)
        self.assertIn("g1", section)

    def test_section_excludes_node_scope_decisions(self):
        records, _ = ingest_worker_decisions(self.paths, "run-1", "g1", [{
            "question": _QUESTION,
            "options": ["要", "不要"],
        }])
        self._answer(records[0])
        self.assertEqual(self._section(), "")

    def test_section_unknown_line_when_ledger_unreadable(self):
        results = self.root / ".vibe" / "decisions" / "results"
        results.mkdir(parents=True)
        os.rmdir(results)
        os.symlink(str(self.home), str(results))
        self.assertEqual(
            self._section(), "本 run 已答决策台账不可读（unknown），已答决策未注入")

    # -- h. CLI decision-raise reuse path -----------------------------------
    def test_cli_decision_raise_reuses_answered(self):
        first = run_cli([
            "decision-raise", "--json", "--run-id", "run-1", "--node", "g1",
            "--question", _QUESTION, "--options", '["要", "不要"]',
        ], cwd=self.root)
        self.assertEqual(first.exit_code, SUCCESS, first.payload)
        self.assertEqual(first.payload["request"]["scope"], "run")
        decision_id = first.payload["request"]["decision_id"]
        answer_decision(
            self.paths, decision_id, "要",
            request_digest=first.payload["request"]["request_digest"])
        second = run_cli([
            "decision-raise", "--json", "--run-id", "run-1", "--node", "g2",
            "--question", _QUESTION, "--options", '["要", "不要"]',
        ], cwd=self.root)
        self.assertEqual(second.exit_code, SUCCESS, second.payload)
        self.assertTrue(second.payload["reused"])
        self.assertEqual(second.payload["request"]["decision_id"], decision_id)
        self.assertEqual(second.payload["choice"], "要")
        self.assertEqual(len(self._requests_files()), 1)

    # -- F3: reuse requires the old choice to be among the new options -------
    def test_reuse_rejected_when_old_choice_not_in_new_options(self):
        first = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        self._answer(first["request"], choice="要")
        second = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g2",
            question=_QUESTION, options=["必须", "可选"])
        self.assertFalse(second["reused"])
        self.assertEqual(len(self._requests_files()), 2)

    def test_reuse_kept_when_old_choice_in_new_options(self):
        first = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        self._answer(first["request"], choice="要")
        second = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g2",
            question=_QUESTION, options=["暂缓", "要", "不要"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["choice"], "要")
        self.assertEqual(len(self._requests_files()), 1)

    # -- F1: first-dispatch create prompts carry answered run decisions ------
    def _raise_and_answer_run_decision(self, run_id="run-1"):
        first = raise_reusable_decision(
            self.paths, run_id=run_id, node_id="g1",
            question=_QUESTION, options=["要", "不要"])
        self._answer(first["request"])
        return first["request"]["decision_id"]

    def _create_prompt(self, contract, runner_paths=None):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        runner = ProviderActionRunner(
            runner_paths or ProjectPaths(Path(tempfile.mkdtemp())),
            "codex", "codex-app-visible")
        seen = []

        def fake(c, r, operation, request):
            seen.append((operation, request))
            return {"create": {"binding": {"task_id": "t", "host": "mac"}},
                    "locate": {"located": True},
                    "visibility": {"visible": True, "direct_enter": True}}[operation]

        runner._require_result = fake
        runner.task_binding(contract, Path(tempfile.mkdtemp()), "run-1", "running")
        return dict(seen)["create"]["prompt"]

    def test_generic_create_prompt_injects_answered_run_decisions(self):
        decision_id = self._raise_and_answer_run_decision()
        prompt = self._create_prompt({
            "run_id": "run-1", "node_id": "g2", "role": "developer",
            "generation": 1, "project_id": "p", "topology": "dual-visible",
            "worktree": "w", "branch": "b", "files": [],
            "project_root": str(self.root),
        })
        self.assertIn(decision_id, prompt)
        self.assertIn(_QUESTION, prompt)
        self.assertIn("结论：要", prompt)
        self.assertIn("同一问题不要重复提问", prompt)

    def test_visible_sdd_create_prompt_injects_answered_run_decisions(self):
        decision_id = self._raise_and_answer_run_decision()
        prompt = self._create_prompt({
            "run_id": "run-1", "node_id": "g2", "role": "developer",
            "generation": 1, "project_id": "p", "topology": "visible-sdd",
            "sdd_protocol": "vibe_guide/protocols/visible-sdd-worker.md",
            "worktree": "w", "branch": "b", "files": [],
            "project_root": str(self.root),
        })
        self.assertIn(decision_id, prompt)
        self.assertIn(_QUESTION, prompt)
        self.assertIn("结论：要", prompt)

    def test_create_prompt_without_answered_decisions_unchanged_shape(self):
        prompt = self._create_prompt({
            "run_id": "run-1", "node_id": "g2", "role": "developer",
            "generation": 1, "project_id": "p", "topology": "dual-visible",
            "worktree": "w", "branch": "b", "files": [],
            "project_root": str(self.root),
        })
        self.assertNotIn("本 run 已答决策", prompt)

    def test_cli_decision_raise_scope_node_flag(self):
        first = run_cli([
            "decision-raise", "--json", "--run-id", "run-1", "--node", "g1",
            "--question", _QUESTION, "--options", '["要", "不要"]',
            "--scope", "node",
        ], cwd=self.root)
        self.assertEqual(first.exit_code, SUCCESS, first.payload)
        self.assertEqual(first.payload["request"]["scope"], "node")


if __name__ == "__main__":
    unittest.main()
