"""wake-and-handoff: every way a worker's turn ends reaches the supervisor, and shifts stay cheap.

Acceptance anchors (PRD run-recovery-5-2-5-r2):
1/2. end-of-turn wake in the dispatch text, on worker-deliver refusals, and a
     beat that never reads idle while a node waits for the supervisor.
3.   a rotate beat with a pending mailbox services it first, rotates next beat.
6.   the supervisor card stays within 3072 bytes.
7.   every session-creating call site passes ``model`` explicitly.
Also: a timeout that moved no cursor queues the next non-blocking wait itself,
and the handoff names model/effort, current blockers, known exits and the
mailbox field table.
"""
import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import protocol_path
from vibe_guide.supervisor import (
    SUPERVISOR_CARD_BYTE_LIMIT,
    SUPERVISOR_SUMMARY_RULES,
    register_supervisor_address,
    supervisor_beat,
    supervisor_preflight,
)

ROOT = Path(__file__).resolve().parent.parent
RUN = "run-wake"

_NO_REPEAT = patch("vibe_guide.supervisor.BEAT_REPEAT_WINDOW_SECONDS", 0)


def setUpModule():
    _NO_REPEAT.start()


def tearDownModule():
    _NO_REPEAT.stop()


def _record(directory, name, tokens):
    path = Path(directory) / (name + ".jsonl")
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": name}}) + "\n"
                    + json.dumps({"context_tokens": tokens}) + "\n", encoding="utf-8")
    return path


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".vibe").mkdir(parents=True)
        self.paths = ProjectPaths(self.root)


class DispatchTextTests(unittest.TestCase):
    def test_continuation_prompt_covers_every_ending(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        prompt = ProviderActionRunner._continuation_prompt(
            {"node_id": "n1", "role": "developer", "generation": 2, "run_id": RUN}, Path("/proj"))
        for ending in ("交付", "交付被拒", "提问", "阻塞"):
            self.assertIn(ending, prompt)
        self.assertIn("任何结束回合之前", prompt)
        self.assertIn("worker-deliver 拒收时返回值已带 `wake` 指令", prompt)

    def test_create_prompt_covers_every_ending(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        with tempfile.TemporaryDirectory() as d:
            runner = ProviderActionRunner(ProjectPaths(Path(d)), "codex", "codex-app-visible")
            seen = []

            def fake(contract, run_id, operation, request):
                seen.append((operation, request))
                return {"create": {"binding": {"task_id": "t", "host": "mac"}},
                        "locate": {"located": True},
                        "visibility": {"visible": True, "direct_enter": True}}[operation]

            runner._require_result = fake
            runner.task_binding({"node_id": "n1", "role": "developer", "generation": 1,
                                 "project_id": "p", "topology": "dual-visible",
                                 "worktree": "w", "branch": "b", "files": []},
                                Path(d), RUN, "running")
            prompt = dict(seen)["create"]["prompt"]
        self.assertIn("任何结束回合之前", prompt)
        self.assertIn("交付被拒", prompt)
        self.assertIn("worker-deliver 拒收时返回值已带 `wake` 指令", prompt)


class WorkerDeliverRefusalTests(_Case):
    def test_refusal_carries_the_wake_instruction(self):
        register_supervisor_address(
            self.paths, RUN, {"provider": "codex-app-visible", "session_id": "SUP", "host": "mac"})
        out = run_cli(["worker-deliver", "--run-id", RUN, "--node", "n1", "--role", "developer",
                       "--generation", "1", "--payload", "{}", "--json"], self.root)
        self.assertEqual(out.payload["status"], "blocked_invalid")
        wake = out.payload["wake"]
        self.assertEqual(wake["channel"], "send_message_to_thread")
        self.assertEqual(wake["target"], "SUP")
        self.assertEqual(wake["signal"], "vibe-wake run=run-wake node=n1 role=developer generation=1")
        self.assertIn(wake["signal"], out.text)

    def test_refusal_without_a_registered_supervisor_still_refuses(self):
        out = run_cli(["worker-deliver", "--run-id", RUN, "--node", "n1", "--role", "developer",
                       "--generation", "1", "--payload", "{}", "--json"], self.root)
        self.assertEqual(out.payload["status"], "blocked_invalid")
        self.assertNotIn("wake", out.payload)


QUESTION = {"node_id": "n1", "role": "developer", "generation": 1, "kind": "scope_landing",
            "file": ".vibe/runs/run-wake/worker-questions/n1-developer-g1.json",
            "excerpt": "交付被拒：越界文件 x.py"}


class AwaitingSupervisorBeatTests(_Case):
    def beat(self, mailbox, questions=(QUESTION,)):
        record = _record(self.tmp.name, "sup", 1000)
        register_supervisor_address(
            self.paths, RUN, {"provider": "codex-app-visible", "session_id": "sup", "host": "mac"},
            session_record=record)
        with patch("vibe_guide.supervisor._mailbox_state", return_value=mailbox), \
                patch("vibe_guide.supervisor._awaiting_questions", return_value=list(questions)):
            return supervisor_beat(self.paths, RUN, record, accept_decision=True)

    def test_idle_mailbox_with_an_awaiting_node_is_a_decision_with_the_summary(self):
        payload, text, ok = self.beat({"state": "idle", "reason": "workers active"})
        self.assertEqual(payload["state"], "decision")
        self.assertTrue(ok)
        self.assertFalse(payload["end_turn"])
        self.assertIn("交付被拒：越界文件 x.py", text)
        self.assertEqual(payload["worker_questions"][0]["node_id"], "n1")

    def test_work_beat_carries_the_summary_too(self):
        payload, text, _ok = self.beat({"state": "work", "reason": "pending provider requests"})
        self.assertEqual(payload["state"], "work")
        self.assertIn("交付被拒：越界文件 x.py", text)

    def test_no_awaiting_node_stays_idle(self):
        payload, _text, _ok = self.beat({"state": "idle", "reason": "workers active"}, questions=())
        self.assertEqual(payload["state"], "idle")

    def test_repeat_window_does_not_hide_an_awaiting_node(self):
        record = _record(self.tmp.name, "sup", 1000)
        register_supervisor_address(
            self.paths, RUN, {"provider": "codex-app-visible", "session_id": "sup", "host": "mac"},
            session_record=record)
        with patch("vibe_guide.supervisor.BEAT_REPEAT_WINDOW_SECONDS", 3600), \
                patch("vibe_guide.supervisor._mailbox_state",
                      return_value={"state": "idle", "reason": "workers active"}), \
                patch("vibe_guide.supervisor._awaiting_questions", return_value=[QUESTION]):
            supervisor_beat(self.paths, RUN, record, accept_decision=True)
            second, _text, _ok = supervisor_beat(self.paths, RUN, record, accept_decision=True)
        self.assertEqual(second["state"], "decision")


class RotateDeferralTests(_Case):
    def preflight(self, backlog):
        record = _record(self.tmp.name, "sup", 250000)
        register_supervisor_address(
            self.paths, RUN, {"provider": "codex-app-visible", "session_id": "sup", "host": "mac"},
            session_record=record)
        record.write_text(json.dumps({"context_tokens": 250000}), encoding="utf-8")
        with patch("vibe_guide.supervisor._mailbox_backlog", return_value=backlog):
            return supervisor_preflight(self.paths, RUN, record)

    def test_pending_actions_defer_the_rotation_one_beat(self):
        first = self.preflight((1, []))
        self.assertEqual(first["state"], "work")
        self.assertEqual(first["rotate_deferred"]["pending_count"], 1)
        self.assertEqual(first["rotate_deferred"]["would_rotate_for"], "context over hard cap")
        second = self.preflight((1, []))
        self.assertEqual(second["state"], "rotate")

    def test_unconsumed_delivery_defers_too(self):
        first = self.preflight((0, ["n1"]))
        self.assertEqual(first["state"], "work")
        self.assertEqual(first["rotate_deferred"]["unconsumed_deliveries"], ["n1"])

    def test_empty_mailbox_rotates_at_once(self):
        self.assertEqual(self.preflight((0, []))["state"], "rotate")

    def test_beat_text_reports_the_pending_work_before_the_rotation(self):
        record = _record(self.tmp.name, "sup", 250000)
        register_supervisor_address(
            self.paths, RUN, {"provider": "codex-app-visible", "session_id": "sup", "host": "mac"},
            session_record=record)
        record.write_text(json.dumps({"context_tokens": 250000}), encoding="utf-8")
        with patch("vibe_guide.supervisor._mailbox_backlog", return_value=(2, [])), \
                patch("vibe_guide.supervisor.supervisor_handoff", return_value=({"handoff_summary": {}}, "")), \
                patch("vibe_guide.supervisor._card_block", return_value=("card", {"path": "p", "sha256": "s"}, True)):
            payload, text, _ok = supervisor_beat(self.paths, RUN, record)
        self.assertEqual(payload["state"], "work")
        self.assertIn("下一拍再换班", text)
        self.assertIn("2 项待服务请求", text)


class TimeoutContinuationTests(_Case):
    def _runner(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        runner = ProviderActionRunner(self.paths, "codex", "codex-app-visible")
        runner.store._atomic = lambda path, data: None
        return runner

    def _handle_metadata(self, cursor):
        meta = {"node_id": "n1", "role": "developer", "task_id": "t", "host": "mac",
                "run_id": RUN, "generation": 1, "wait_sequence": 3}
        if cursor:
            meta["cursor"] = cursor
        return meta

    def _timeout(self, result, cursor):
        runner = self._runner()
        issued = []

        def fake_issue(handle, metadata):
            issued.append(dict(metadata))
            return {"action_id": "a"}

        runner._issue_wait = fake_issue
        handle = type("H", (), {"run_id": RUN})()
        meta = self._handle_metadata(cursor)
        claims = {"node_id": "n1", "role": "developer", "task_id": "t", "handle_id": RUN, "generation": 1}
        with patch("vibe_guide.runners.provider_action.load_task_binding"), \
                patch("vibe_guide.runners.provider_action.save_task_binding"):
            events = runner._wait_result(handle, meta, claims, result)
        return events, issued

    def test_timeout_with_unchanged_cursor_queues_the_next_wait(self):
        events, issued = self._timeout({"status": "timeout", "cursor": "c1"}, "c1")
        self.assertEqual(events[0].event, "visibility_unknown")
        self.assertEqual(len(issued), 1)

    def test_timeout_without_a_cursor_also_continues(self):
        _events, issued = self._timeout({"status": "timeout"}, "c1")
        self.assertEqual(len(issued), 1)

    def test_timeout_with_a_moved_cursor_does_not_auto_continue(self):
        _events, issued = self._timeout({"status": "timeout", "cursor": "c2"}, "c1")
        self.assertEqual(issued, [])

    def test_issued_wait_is_non_blocking_from_the_same_cursor(self):
        runner = self._runner()
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.contracts import RunEvent
        from vibe_guide.state import append_event
        store = ProviderActionStore(self.paths)
        (self.paths.vibe / "state.json").write_text("{}")
        with patch("vibe_guide.adapters.task_provider.require_entry"):
            action = store.request(operation="create", provider=runner.provider, run_id=RUN,
                issue_id="n1", role="developer", generation=1,
                native_tool="codex_app__create_thread", request={})
        append_event(self.paths, RunEvent("orphan_dispatch_adopted", {
            "run_id": RUN, "node_id": "n1", "role": "developer", "task_id": "t",
            "host": "mac", "generation": 1, "handle_id": RUN,
            "action_id": action["action_id"], "request_digest": action["request_digest"],
        }))
        captured = {}

        def fake_action(contract, run_id, operation, request, sequence=None):
            captured.update(operation=operation, request=request, sequence=sequence)
            return {"action_id": "a-1"}

        runner._action = fake_action
        handle = type("H", (), {"run_id": RUN})()
        meta = self._handle_metadata("c1")
        runner._issue_wait(handle, meta)
        self.assertEqual(captured["operation"], "wait")
        self.assertEqual(captured["request"]["timeoutMs"], 0)
        self.assertEqual(captured["request"]["targets"][0]["afterCursor"], "c1")
        self.assertEqual(captured["sequence"], 4)
        self.assertEqual(meta["pending_action"], "a-1")


class HandoffContentTests(_Case):
    def test_handoff_names_model_blockers_exits_and_field_table(self):
        from vibe_guide.supervisor import supervisor_handoff
        from vibe_guide.state import RunSnapshot

        record = _record(self.tmp.name, "sup", 1000)
        with open(record, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "turn_context", "payload": {"model": "gpt-5", "effort": "high"}}) + "\n")
        register_supervisor_address(
            self.paths, RUN, {"provider": "codex-app-visible", "session_id": "sup", "host": "mac"},
            session_record=record)
        snapshot = RunSnapshot(run_id=RUN, plan_id="plan-1", plan_version=1, status="running",
                               nodes={"n1": {"status": "blocked_unknown", "reason": "worker 无响应"},
                                      "n2": {"status": "accepted"}},
                               handles={})
        directory = self.paths.vibe / "runs" / RUN
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tasks.json").write_text(json.dumps({"bindings": []}), encoding="utf-8")
        with patch("vibe_guide.supervisor.load_snapshot", return_value=snapshot), \
                patch("vibe_guide.supervisor._snapshot_refs", return_value=[]), \
                patch("vibe_guide.supervisor._write_handoff_summary",
                      return_value={"path": "p", "sha256": "s"}):
            payload, text = supervisor_handoff(self.paths, RUN)
        self.assertIn("模型 gpt-5，思考档位 high", text)
        self.assertIn("n1（blocked_unknown：worker 无响应）", text)
        self.assertIn("已知出口：", text)
        self.assertIn("requeue", text)
        self.assertIn("MAILBOX_FIELD_TABLE", text)
        self.assertEqual(payload["successor_session"]["model"], "gpt-5")

    def test_summary_carries_the_rules_and_the_no_authorize_line(self):
        from vibe_guide.supervisor import (
            _current_blockers, _successor_session, MAILBOX_FIELD_TABLE_REF, SUPERVISOR_KNOWN_EXITS)

        self.assertTrue(any("监工不执行 `monitor --authorize`" in rule for rule in SUPERVISOR_SUMMARY_RULES))
        self.assertIn("MAILBOX_FIELD_TABLE", MAILBOX_FIELD_TABLE_REF)
        self.assertTrue(SUPERVISOR_KNOWN_EXITS)
        self.assertEqual(_successor_session(self.paths, RUN)["source"], "unrecorded")
        self.assertEqual(_current_blockers(self.paths, None, {"a": {"status": "accepted"}}, {}), [])


class CardBudgetTests(unittest.TestCase):
    def test_supervisor_card_fits_the_byte_budget(self):
        size = len(protocol_path("supervisor-card").read_bytes())
        self.assertLessEqual(size, SUPERVISOR_CARD_BYTE_LIMIT, size)
        self.assertEqual(SUPERVISOR_CARD_BYTE_LIMIT, 3072)


def _calls(path, names):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in names:
                found.append((name, node.lineno, {kw.arg for kw in node.keywords}))
    return found


class ModelPassedExplicitlyTests(unittest.TestCase):
    """Every call that creates a session says which model, even when the answer is None."""

    def test_background_launcher_construction_passes_model(self):
        calls = _calls("vibe_guide/cli.py", {"ClaudeBgLauncher"})
        self.assertTrue(calls)
        for name, line, keywords in calls:
            self.assertIn("model", keywords, "cli.py:%d %s" % (line, name))

    def test_daemon_short_session_launch_passes_model(self):
        calls = _calls("vibe_guide/daemon.py", {"launcher"})
        self.assertTrue(calls)
        for name, line, keywords in calls:
            self.assertIn("model", keywords, "daemon.py:%d %s" % (line, name))

    def test_short_session_command_build_passes_model(self):
        tree = ast.parse((ROOT / "vibe_guide/short_session.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "build_command"]
        self.assertTrue(calls)
        for node in calls:
            self.assertGreaterEqual(len(node.args), 3, "build_command must receive model positionally")

    def test_native_create_request_carries_model(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        with tempfile.TemporaryDirectory() as d:
            runner = ProviderActionRunner(ProjectPaths(Path(d)), "codex", "codex-app-visible")
            seen = []

            def fake(contract, run_id, operation, request):
                seen.append((operation, request))
                return {"create": {"binding": {"task_id": "t", "host": "mac"}},
                        "locate": {"located": True},
                        "visibility": {"visible": True, "direct_enter": True}}[operation]

            class Profile:
                model = "gpt-5"
                reasoning = "deep"
                route_digest = "d" * 64
                selection_basis = ()

                def to_dict(self):
                    return {"model": self.model, "reasoning": self.reasoning, "route_digest": self.route_digest}

            runner._require_result = fake
            with patch.object(ProviderActionRunner, "_routing_profile", return_value=Profile()):
                runner.task_binding({"node_id": "n1", "role": "developer", "generation": 1,
                                     "project_id": "p", "topology": "dual-visible",
                                     "worktree": "w", "branch": "b", "files": []},
                                    Path(d), RUN, "running")
            request = dict(seen)["create"]
        self.assertEqual(request["model"], "gpt-5")
        self.assertEqual(request["thinking"], "high")


if __name__ == "__main__":
    unittest.main()
