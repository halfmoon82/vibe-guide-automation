"""A background worker that stops to ask the supervisor is surfaced, not parked.

Recorded failure (run-5e9174c5, 2026-10-28): twice a background developer
ended its turn with a question for the supervisor (once asking to widen its
file scope, once asking which code baseline to start from) and wrote no
delivery.  vibe reported ``blocked_unknown`` / ``binding_unknown`` and the
supervisor only found the question by reading the session transcript by hand.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide import worker_questions
from vibe_guide.adapters.claude_bg import ClaudeBgUnknown
from vibe_guide.contracts import RunHandle
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.state import load_events, load_snapshot, map_user_status
from vibe_guide.supervisor import render_beat_text, supervisor_preflight

# Imported as a module: a test class bound at module level would run again here.
from tests import test_claude_bg_delivery_gate as _gate
from tests.test_claude_bg_reconnect import UUID

QUESTION = (
    "我已读完合同。上游节点的代码不在当前基线里，需要监工决定：从 main 开工，"
    "还是先合并 node/shared-foundation？确认后我就开工。"
)


class QuestionLauncher:
    """Fake launcher whose session ended with ``reply`` as its last message."""

    def __init__(self, state="working", reply=None, calls=None):
        self.state = state
        self.reply = reply
        self.calls = calls if calls is not None else []

    def launch(self, role, issue_id, contract_path):
        self.calls.append(("launch", role, issue_id, str(contract_path)))
        return {"task_id": UUID}

    def status(self, task_id):
        return self.state

    def final_reply(self, task_id):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def stop(self, task_id):
        self.calls.append(("stop", task_id))
        self.state = "stopped"

    def resume(self, task_id, message):
        self.calls.append(("resume", task_id, message))


class RunnerQuestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = ProjectPaths(Path(temporary.name))
        self.contract = {"node_id": "n1", "role": "developer", "run_id": "r1",
                         "generation": 1, "contract_path": "/c.md"}

    def poll(self, launcher):
        runner = ClaudeBgRunner(lambda c, w: launcher, delivery_reader=lambda claims: None,
                                paths=self.paths)
        return runner.poll(runner.start(dict(self.contract), Path("/tmp")))

    def test_ended_session_without_delivery_reports_its_last_message(self):
        for state in ("done", "idle", "blocked"):
            events = self.poll(QuestionLauncher(state, QUESTION))
            self.assertEqual([event.event for event in events], ["worker_question"], state)
            data = events[0].data
            self.assertEqual(data["question_excerpt"], QUESTION)
            self.assertEqual(data["session_state"], state)
            self.assertEqual((data["node_id"], data["generation"]), ("n1", 1))

    def test_excerpt_keeps_the_end_of_a_long_message(self):
        reply = "背景" * 500 + QUESTION
        data = self.poll(QuestionLauncher("done", reply))[0].data
        self.assertLessEqual(len(data["question_excerpt"]), worker_questions.EXCERPT_LIMIT)
        self.assertTrue(data["question_excerpt"].endswith("确认后我就开工。"))

    def test_without_a_readable_last_message_the_old_events_stay(self):
        for reply in (None, "  ", ClaudeBgUnknown("cannot read transcript: gone")):
            events = self.poll(QuestionLauncher("done", reply))
            self.assertEqual([event.event for event in events], ["blocked_unknown"], reply)
        self.assertEqual([e.event for e in self.poll(QuestionLauncher("blocked", None))],
                         ["needs_attention"])

    def test_a_working_session_is_never_asked(self):
        self.assertEqual(self.poll(QuestionLauncher("working", QUESTION)), [])


class MonitorQuestionTests(unittest.TestCase):
    """Drives the gated background path: start -> question -> answer -> redispatch."""

    # The gated vibeguide_monitor fixture, without re-running its own tests.
    setUp = _gate.BackgroundDeliveryGateTests.setUp
    tearDown = _gate.BackgroundDeliveryGateTests.tearDown
    _engine_plan = _gate.BackgroundDeliveryGateTests._engine_plan
    monitor = _gate.BackgroundDeliveryGateTests.monitor
    start = _gate.BackgroundDeliveryGateTests.start

    def runner(self, state="working", reply=None, calls=None, node="bg"):
        # Only ``node`` is in ``state``; every other node keeps working.
        return ClaudeBgRunner(
            lambda c, w: QuestionLauncher(state if c["node_id"] == node else "working",
                                          reply, calls),
            paths=self.paths, sleeper=lambda seconds: None)

    def ask(self):
        snapshot = self.start()
        return self.monitor().resume(snapshot.run_id, self.runner("done", QUESTION))

    def test_question_parks_the_node_for_the_supervisor_not_blocked_unknown(self):
        snapshot = self.ask()
        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], worker_questions.AWAITING_STATUS)
        self.assertNotEqual(node["status"], "blocked_unknown")
        self.assertIsNone(node.get("active_task"))
        self.assertNotIn("bg", snapshot.handles)
        self.assertEqual(map_user_status(node), "需要监工排查")
        question = worker_questions.pending_questions(self.paths, snapshot)
        self.assertEqual([item["node_id"] for item in question], ["bg"])
        self.assertEqual(question[0]["excerpt"], QUESTION)
        self.assertTrue((self.paths.root / question[0]["file"]).is_file())
        # The excerpt lives in its own file, never in the redacted event stream.
        events = json.dumps(load_events(self.paths, snapshot.run_id), ensure_ascii=False)
        self.assertNotIn("确认后我就开工", events)
        self.assertIn("worker_question_raised", events)
        # Survives a reload from disk.
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).nodes["bg"]["status"],
                         worker_questions.AWAITING_STATUS)
        # The other node never asked and keeps running.
        self.assertEqual(snapshot.nodes["bg2"]["status"], "running")

    def test_the_beat_raises_a_decision_with_the_excerpt_and_the_file(self):
        snapshot = self.ask()
        record = Path(self.temporary.name) / "supervisor.jsonl"
        record.write_text(json.dumps({"context_tokens": 1000}) + "\n", encoding="utf-8")
        with patch("vibe_guide.adapters.task_provider.require_entry"):
            payload = supervisor_preflight(self.paths, snapshot.run_id, record,
                                           accept_decision=True)
        self.assertEqual(payload["state"], "decision", payload)
        [question] = payload["worker_questions"]
        self.assertEqual((question["node_id"], question["excerpt"]), ("bg", QUESTION))
        self.assertTrue(question["file"].endswith(".json"))
        text = render_beat_text(self.paths, snapshot.run_id, payload, plan_id="plan-bg")
        self.assertIn("确认后我就开工", text)
        self.assertIn(question["file"], text)
        self.assertIn("requeue --plan plan-bg --run-id {} --node bg --note".format(snapshot.run_id), text)

    def test_answer_redispatches_the_same_node_with_the_answer_in_the_prompt(self):
        snapshot = self.ask()
        answer = "先合并 node/shared-foundation 再开工，范围不变。"
        result = worker_questions.answer_question(self.monitor(), snapshot.run_id, "bg", answer)
        self.assertEqual(result["status"], "rework")
        stored = json.loads((self.paths.root / result["file"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["answer"], answer)
        self.assertEqual(worker_questions.pending_questions(
            self.paths, load_snapshot(self.paths, snapshot.run_id)), [])

        calls = []
        snapshot = self.monitor().resume(snapshot.run_id, self.runner(calls=calls))

        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "rework", node.get("reason"))
        self.assertEqual(node["active_task"]["role"], "developer")
        self.assertIn("bg", snapshot.handles)
        self.assertEqual(node["developer_generation"], 2)
        launches = [call for call in calls if call[0] == "launch"]
        self.assertEqual(len(launches), 1, calls)
        prompt = Path(launches[0][3]).read_text(encoding="utf-8")
        self.assertIn(answer, prompt)
        self.assertIn("确认后我就开工", prompt)

    def test_answer_requires_text_and_an_open_question(self):
        snapshot = self.ask()
        with self.assertRaises(ValueError):
            worker_questions.answer_question(self.monitor(), snapshot.run_id, "bg", "  ")
        with self.assertRaises(ValueError):
            worker_questions.answer_question(self.monitor(), snapshot.run_id, "bg2", "go")

    def test_the_ended_session_is_stopped_before_its_binding_turns_terminal(self):
        from vibe_guide.task_registry import load_task_binding

        snapshot = self.start()
        calls = []
        snapshot = self.monitor().resume(snapshot.run_id, self.runner("idle", QUESTION, calls))
        self.assertIn(("stop", UUID), calls)
        binding = load_task_binding(self.paths, "bg", "developer", run_id=snapshot.run_id)
        self.assertEqual(binding.status, "stopped")

    def test_a_stop_that_cannot_be_proven_keeps_the_node_blocked_unknown(self):
        class Unstoppable(QuestionLauncher):
            def stop(self, task_id):
                raise ClaudeBgUnknown("claude stop failed")

        snapshot = self.start()
        runner = ClaudeBgRunner(
            lambda c, w: Unstoppable("blocked" if c["node_id"] == "bg" else "working", QUESTION),
            paths=self.paths, sleeper=lambda seconds: None)
        snapshot = self.monitor().resume(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["bg"]["status"], "blocked_unknown")
        self.assertEqual(worker_questions.pending_questions(self.paths, snapshot), [])

    def _replay_after_lost_save(self, before):
        """Put back the snapshot saved before the last step, as a crash would."""
        (self.paths.vibe / "runs" / self.run_id / "state.json").write_bytes(before)
        snapshot = load_snapshot(self.paths, self.run_id)
        self.monitor()._reconcile_unapplied_events(snapshot)
        return snapshot

    def test_a_lost_snapshot_save_after_the_question_replays_to_awaiting(self):
        snapshot = self.start()
        self.run_id = snapshot.run_id
        before = (self.paths.vibe / "runs" / self.run_id / "state.json").read_bytes()
        self.monitor().resume(snapshot.run_id, self.runner("done", QUESTION))

        replayed = self._replay_after_lost_save(before)

        self.assertEqual(replayed.nodes["bg"]["status"], worker_questions.AWAITING_STATUS)
        self.assertEqual(worker_questions.pending_questions(self.paths, replayed)[0]["excerpt"],
                         QUESTION)

    def test_a_lost_snapshot_save_after_the_answer_replays_to_rework(self):
        snapshot = self.ask()
        self.run_id = snapshot.run_id
        before = (self.paths.vibe / "runs" / self.run_id / "state.json").read_bytes()
        worker_questions.answer_question(self.monitor(), self.run_id, "bg", "按 main 开工")

        replayed = self._replay_after_lost_save(before)

        node = replayed.nodes["bg"]
        self.assertEqual(node["status"], "rework")
        self.assertEqual(node["retryable_action"]["role"], "developer")
        self.assertTrue(node["retryable_action"]["pending_schedule"])

    def test_credentials_in_the_last_message_never_reach_the_excerpt(self):
        reply = "我卡住了，token=ghp_abcdefghijklmnopqrstuvwxyz0123 和 sk-ABCDEFGHIJKLMNOPQRSTUV 都不行，要不要换？"
        text = worker_questions.excerpt(reply)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123", text)
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUV", text)
        self.assertIn("要不要换？", text)

    def test_cli_requeue_with_a_note_answers_the_question(self):
        from vibe_guide.cli import run_cli

        snapshot = self.ask()
        loaded = (self.paths.vibe / "plans" / "plan-bg", self.plan, self.nodes, None)
        with patch("vibe_guide.cli._load_plan", return_value=loaded), \
                patch("vibe_guide.cli._observed_topology_rulings", return_value=None):
            refused = run_cli(["requeue", "--plan", "plan-bg", "--run-id", snapshot.run_id,
                               "--node", "bg", "--json"], self.paths.root)
            answered = run_cli(["requeue", "--plan", "plan-bg", "--run-id", snapshot.run_id,
                                "--node", "bg", "--note", "按 main 开工", "--json"],
                               self.paths.root)
        self.assertIn("答复内容为空", refused.payload["reason"])
        self.assertEqual(answered.payload["repair"]["status"], "rework", answered.payload)
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).nodes["bg"]["status"], "rework")

    def test_answer_section_only_reaches_the_next_generation(self):
        snapshot = self.ask()
        answer = "按 main 开工"
        worker_questions.answer_question(self.monitor(), snapshot.run_id, "bg", answer)
        base = {"run_id": snapshot.run_id, "node_id": "bg", "role": "developer",
                "project_root": str(self.paths.root)}
        self.assertIn(answer, worker_questions.supervisor_answer_section(dict(base, generation=2)))
        self.assertEqual(worker_questions.supervisor_answer_section(dict(base, generation=3)), "")
        self.assertEqual(worker_questions.supervisor_answer_section(
            dict(base, generation=2, role="reviewer")), "")


if __name__ == "__main__":
    unittest.main()
