import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide import daemon as dm
from vibe_guide import short_session as ss
from vibe_guide.cli import run_cli
from vibe_guide.decision_boundary import decision_list
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events, run_dir
from vibe_guide.supervisor import SupervisorLeaseConflict

RUN = "run-" + "a" * 32
NODE = "n1"


class FakeLease:
    def __init__(self):
        self.released = False

    def release(self, run_id):
        self.released = True


class FakeSupervisor:
    """Stands in for Supervisor: a scripted list of run statuses, one per run_once."""

    def __init__(self, statuses, active=True, error=None):
        self.statuses = list(statuses)
        self.active = active
        self.error = error
        self.lease = FakeLease()
        self.calls = 0

    def recover_or_start(self):
        return {"active_supervisors": 1 if self.active else 0}

    def run_once(self):
        self.calls += 1
        if self.error:
            raise self.error
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return SimpleNamespace(status=status, nodes={NODE: {"status": "running"}}, tasks={},
                               authorization={"scope": "demo"})


def stalled(tier="supervisor_investigate"):
    return {"status": "ok", "nodes": {NODE: {"tier": tier, "pack": {"role": "developer", "kind": "silent_worker"}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name), vibe_home=Path(self.tmp.name) / "home")
        (Path(self.tmp.name) / "home").mkdir()
        run_dir(self.paths, RUN, create=True)
        self.pushed = []
        self.archived = []
        self.sessions = []
        self.session_results = []
        self.verify_calls = []

    def notifier(self, message, kind, node_id=""):
        self.pushed.append((kind, node_id, message))
        return {"status": "sent"}

    def launcher(self, platform, prompt, **kwargs):
        self.sessions.append((platform, prompt, kwargs))
        if self.session_results:
            return self.session_results.pop(0)
        return ss.ShortSessionResult(platform, "ok", text="VIBE_HEAL: not_fixed still silent")

    def verify(self, node_id, role):
        self.verify_calls.append((node_id, role))
        return {"status": "verify_failed"}

    def daemon(self, supervisor, assess=None, **config):
        values = {"interval": 0, "heal_interval": 0}
        values.update(config)
        self.retries = []
        return dm.Daemon(
            self.paths, RUN, supervisor, dm.DaemonConfig(**values),
            sleep=lambda s: None, launcher=self.launcher, notifier=self.notifier,
            assess=assess or (lambda: {"status": "ok", "nodes": {}}), verify=self.verify,
            retry=lambda node: self.retries.append(node),
            archive=lambda: self.archived.append(RUN), sessions=lambda snapshot: {"live": [], "unknown": None})

    def event_kinds(self):
        return [(e["event"], e["data"].get("kind"), e["data"].get("reason_code")) for e in load_events(self.paths, RUN)]


class ProgressionTests(Base):
    def test_normal_run_never_calls_a_short_session(self):
        def no_launch(*a, **k):
            raise AssertionError("short session must not start on a healthy run")
        daemon = self.daemon(FakeSupervisor(["running", "running", "complete"]))
        daemon.launcher = no_launch
        outcome = daemon.run()
        self.assertEqual(outcome, {"status": "terminal", "run_status": "complete"})
        self.assertEqual(self.sessions, [])
        record = json.loads((run_dir(self.paths, RUN) / dm.DAEMON_FILE).read_text())
        self.assertEqual(record["state"], "exited")
        self.assertEqual(record["cycles"], 3)
        self.assertEqual([p[0] for p in self.pushed], ["run_complete"])
        self.assertTrue(daemon.supervisor.lease.released)

    def test_gate_block_stops_before_any_tick_and_escalates(self):
        supervisor = FakeSupervisor(["running"])
        daemon = self.daemon(supervisor)
        daemon._gate = lambda: {"status": "blocked_design", "reason": "authorization invalidated"}
        outcome = daemon.run()
        self.assertEqual((outcome["status"], outcome["run_status"]), ("blocked", "blocked_design"))
        self.assertEqual(supervisor.calls, 0)
        self.assertTrue(supervisor.lease.released)
        self.assertEqual([p[0] for p in self.pushed], ["escalation"])
        self.assertEqual(decision_list(self.paths, RUN)[0]["category"], "external_authorization")

    def test_non_authorization_gate_block_is_reported_not_escalated(self):
        daemon = self.daemon(FakeSupervisor(["running"]))
        daemon._gate = lambda: {"status": "blocked_unknown", "reason": "gate_error: boom"}
        daemon.run()
        self.assertEqual(decision_list(self.paths, RUN), [])
        self.assertEqual([p[0] for p in self.pushed], ["unresolved_report"])
        self.assertIn("现在的情况", self.pushed[0][2])

    def test_gate_runs_every_tick_and_a_raising_gate_blocks(self):
        supervisor = FakeSupervisor(["running", "running", "running"])
        daemon = self.daemon(supervisor)
        seen = []

        def gate():
            seen.append(1)
            if len(seen) == 3:
                raise OSError("evidence unreadable")
            return None
        daemon._gate = gate
        outcome = daemon.run()
        self.assertEqual(supervisor.calls, 2)
        self.assertEqual(outcome["status"], "blocked")
        self.assertIn("gate_error", outcome["reason"])

    def test_lease_held_by_another_process_is_inactive_with_reason(self):
        daemon = self.daemon(FakeSupervisor(["running"], active=False))
        self.assertEqual(daemon.run()["status"], "lease_held")
        status = dm.daemon_status(self.paths, RUN)
        self.assertEqual((status["status"], status["reason"]), ("inactive", "lease_held_by_other_process"))

    def test_lease_lost_mid_run_stops_without_releasing(self):
        supervisor = FakeSupervisor(["running"], error=SupervisorLeaseConflict("epoch is stale"))
        daemon = self.daemon(supervisor)
        self.assertEqual(daemon.run()["status"], "lease_lost")
        self.assertFalse(supervisor.lease.released)
        status = dm.daemon_status(self.paths, RUN)
        self.assertEqual((status["status"], status["reason"][:5]), ("inactive", "epoch"))

    def test_tick_error_is_recorded_and_the_loop_continues(self):
        supervisor = FakeSupervisor(["running"])
        calls = {"n": 0}
        original = supervisor.run_once

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("provider says no")
            return original()
        supervisor.run_once = flaky
        daemon = self.daemon(supervisor)
        daemon.run(max_cycles=3)
        record = json.loads((run_dir(self.paths, RUN) / dm.DAEMON_FILE).read_text())
        self.assertEqual(record["last_error"]["type"], "ValueError")
        self.assertEqual(record["cycles"], 3)

    def test_repeated_errors_are_recorded_not_escalated(self):
        supervisor = FakeSupervisor(["running"])
        supervisor.run_once = lambda: (_ for _ in ()).throw(ValueError("boom"))
        daemon = self.daemon(supervisor)
        daemon.run(max_cycles=8)
        self.assertEqual(self.pushed, [])
        self.assertEqual(decision_list(self.paths, RUN), [])
        self.assertEqual([k for k in self.event_kinds() if k[1] == "engineering_unresolved"],
                         [("daemon_heal", "engineering_unresolved", "daemon_errors")])


class UnresolvedReportTests(Base):
    def run_stuck(self, **config):
        self.now = [1000.0]
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled(), **config)
        daemon.clock = lambda: self.now[0]
        daemon.run(max_cycles=6)
        return daemon

    def reports(self):
        return [p for p in self.pushed if p[0] == "unresolved_report"]

    def test_silent_before_the_deadline(self):
        daemon = self.run_stuck()
        self.now[0] += dm.MAX_UNRESOLVED_REPORT_SECONDS - 1
        daemon.run(max_cycles=2)
        self.assertEqual(self.pushed, [])

    def test_reports_after_six_hours_once_per_set_of_items(self):
        daemon = self.run_stuck()
        sessions = len(self.sessions)
        self.now[0] += dm.MAX_UNRESOLVED_REPORT_SECONDS + 1
        daemon.run(max_cycles=2)
        self.assertEqual(len(self.reports()), 1)
        self.now[0] += 3 * dm.MAX_UNRESOLVED_REPORT_SECONDS
        daemon.run(max_cycles=2)
        self.assertEqual(len(self.reports()), 1)             # same set: no repeat
        self.assertEqual(len(self.sessions), sessions)       # and no new repair session

    def test_report_uses_product_language_with_fixed_options(self):
        daemon = self.run_stuck(unresolved_report_seconds=60)
        self.now[0] += 61
        daemon.run(max_cycles=1)
        text = self.reports()[0][2]
        for option in dm.UNRESOLVED_OPTIONS:
            self.assertIn(option, text)
        body, expansion = text.rsplit("（展开：", 1)
        self.assertIn("受影响的验收例子", body)
        self.assertNotIn("self_heal_no_progress", body)
        self.assertIn("self_heal_no_progress", expansion)

    def test_terminal_run_reports_unresolved_items_immediately(self):
        self.now = [1000.0]
        daemon = self.daemon(FakeSupervisor(["running", "running", "running", "running", "complete"]),
                             assess=lambda: stalled())
        daemon.clock = lambda: self.now[0]
        daemon.run()
        self.assertEqual(len(self.reports()), 1)

    def test_status_view_lists_the_same_items(self):
        self.run_stuck()
        lines = dm.unresolved_report(self.paths, RUN)
        self.assertEqual(len(lines), 1)
        self.assertIn("再给系统一轮", lines[0])

    def test_report_window_can_be_shortened_never_lengthened(self):
        dm.DaemonConfig(unresolved_report_seconds=60).validate()
        for bad in (0, dm.MAX_UNRESOLVED_REPORT_SECONDS + 1, True):
            with self.assertRaises(ValueError):
                dm.DaemonConfig(unresolved_report_seconds=bad).validate()


class UnresolvedLifecycleTests(Base):
    def heal_state(self):
        return json.loads((run_dir(self.paths, RUN) / dm.HEAL_STATE_FILE).read_text())

    def test_daemon_error_entry_survives_the_heal_pass_that_triggered_it(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon._error_streak = dm._ERROR_ESCALATION_STREAK - 1
        daemon._retry = lambda node: (_ for _ in ()).throw(ValueError("retry broke"))
        daemon.heal_pass(SimpleNamespace(status="running", nodes={NODE: {"status": "running"}}, tasks={}))
        self.assertIn("daemon:daemon_errors", self.heal_state()["unresolved"])

    def test_reappearing_item_is_reported_again(self):
        self.now = [1000.0]
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.clock = lambda: self.now[0]
        daemon.run(max_cycles=6)
        self.now[0] += dm.MAX_UNRESOLVED_REPORT_SECONDS + 1
        daemon.run(max_cycles=1)
        first = len([p for p in self.pushed if p[0] == "unresolved_report"])
        state = self.heal_state()
        state["unresolved"].pop(NODE)                      # fixed in between
        dm._save_run_record(run_dir(self.paths, RUN) / dm.HEAL_STATE_FILE, state)
        daemon._unresolved(NODE, "self_heal_no_progress", "again")  # stalls again, same code
        self.now[0] += dm.MAX_UNRESOLVED_REPORT_SECONDS + 1
        daemon.run(max_cycles=1)
        self.assertEqual(len([p for p in self.pushed if p[0] == "unresolved_report"]), first + 1)

    def test_accepted_or_moved_node_is_not_reported(self):
        state = {"unresolved": {NODE: {"code": "x", "at": 0}}, "nodes": {NODE: {"fingerprint": "old"}}}
        accepted = SimpleNamespace(nodes={NODE: {"status": "accepted"}}, tasks={})
        self.assertEqual(dm.live_unresolved(state, accepted), {})
        moved = SimpleNamespace(nodes={NODE: {"status": "running"}}, tasks={})
        original = dm._node_fingerprint
        dm._node_fingerprint = lambda snapshot, node: "new"
        try:
            self.assertEqual(dm.live_unresolved(state, moved), {})
        finally:
            dm._node_fingerprint = original
        self.assertEqual(list(dm.live_unresolved(state, None)), [NODE])

    def test_daemon_errors_clear_when_ticks_recover(self):
        supervisor = FakeSupervisor(["running"])
        calls = {"n": 0}
        original = supervisor.run_once

        def flaky():
            calls["n"] += 1
            if calls["n"] <= dm._ERROR_ESCALATION_STREAK:
                raise ValueError("boom")
            return original()
        supervisor.run_once = flaky
        daemon = self.daemon(supervisor)
        daemon.run(max_cycles=dm._ERROR_ESCALATION_STREAK + 2)
        self.assertNotIn("daemon:daemon_errors", self.heal_state().get("unresolved", {}))

    def test_gate_block_after_errors_is_still_recorded(self):
        daemon = self.daemon(FakeSupervisor(["running"]))
        daemon._unresolved("", "daemon_errors", "errors")
        daemon._gate = lambda: {"status": "blocked_unknown", "reason": "gate_error: boom"}
        daemon.run()
        self.assertIn("daemon:daemon_gate_blocked", self.heal_state()["unresolved"])

    def test_run_level_item_is_not_described_as_a_missing_acceptance_example(self):
        text = "\n".join(dm.unresolved_lines({"daemon:daemon_errors": {"code": "daemon_errors", "at": 0}}))
        self.assertNotIn("该节点未写验收例子", text)
        self.assertIn("守护程序", text)

    def test_acceptance_example_comes_from_the_node_contract(self):
        supervisor = FakeSupervisor(["running"])
        supervisor.monitor = SimpleNamespace(nodes={NODE: SimpleNamespace(
            contract={"acceptance_example": "输入空表单时提示必填"})})
        daemon = self.daemon(supervisor)
        daemon._unresolved(NODE, "self_heal_no_progress", "stuck")
        self.assertIn("输入空表单时提示必填", "\n".join(dm.unresolved_report(self.paths, RUN)))


class SelfHealTests(Base):
    def test_level1_then_level2_then_recorded_after_two_unfixed_repairs(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.run(max_cycles=6)
        self.assertEqual(self.retries, [NODE])                  # level 1, once
        self.assertEqual(len(self.sessions), 2)                 # level 2, exactly twice
        self.assertEqual(self.verify_calls, [(NODE, "developer")] * 2)  # daemon re-checks itself
        self.assertEqual(self.pushed, [])                       # level 3 never asks the PM
        self.assertEqual(decision_list(self.paths, RUN), [])
        self.assertIn(("daemon_heal", "engineering_unresolved", "self_heal_no_progress"), self.event_kinds())

    def test_session_prompt_carries_protocol_pack_and_close_command(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.run(max_cycles=3)
        platform, prompt, kwargs = self.sessions[0]
        self.assertEqual(platform, "claude-code")
        self.assertIn("heal-verify", prompt)
        self.assertIn("silent_worker", prompt)
        self.assertIn("已授权项目", prompt)
        self.assertIn("--run-id %s --node %s" % (RUN, NODE), prompt)

    def test_model_is_passed_through_from_the_caller(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled(), model="sonnet", platform="codex")
        daemon.run(max_cycles=3)
        self.assertEqual(self.sessions[0][0], "codex")
        self.assertEqual(self.sessions[0][2]["model"], "sonnet")

    def test_session_that_ran_heal_verify_is_not_double_counted(self):
        def launcher(platform, prompt, **kwargs):
            from vibe_guide.contracts import RunEvent
            from vibe_guide.state import append_event
            append_event(self.paths, RunEvent("self_heal_attempt", {
                "run_id": RUN, "node_id": NODE, "outcome": "passed", "differences": []}))
            return ss.ShortSessionResult(platform, "ok", text="VIBE_HEAL: verified")
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.launcher = launcher
        daemon.run(max_cycles=3)
        self.assertEqual(self.verify_calls, [])  # the session's own verify was read, not repeated

    def test_unavailable_session_command_is_recorded_as_unknown(self):
        self.session_results = [ss.ShortSessionResult("claude-code", "unknown", "command not available: claude")]
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.run(max_cycles=3)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.verify_calls, [])
        self.assertIn(("daemon_heal", "engineering_unresolved", "short_session_unknown"), self.event_kinds())
        self.assertEqual(self.pushed, [])
        self.assertEqual(decision_list(self.paths, RUN), [])

    def test_authorization_in_the_session_answer_escalates_right_away(self):
        self.session_results = [ss.ShortSessionResult("claude-code", "ok", text="想了想\nVIBE_ESCALATE: 授权：需要改范围")]
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.run(max_cycles=3)
        self.assertIn(("daemon_heal", "escalated", "needs_decision"), self.event_kinds())
        self.assertEqual(decision_list(self.paths, RUN)[0]["category"], "external_authorization")
        self.assertEqual([p[0] for p in self.pushed].count("escalation"), 1)

    def test_product_decision_category_raises_a_product_card(self):
        self.session_results = [ss.ShortSessionResult("claude-code", "ok", text="VIBE_ESCALATE: 产品决策：验收要变")]
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.run(max_cycles=3)
        self.assertEqual(decision_list(self.paths, RUN)[0]["category"], "product_acceptance")

    def test_escalate_mark_without_a_known_category_stays_with_the_system(self):
        self.session_results = [ss.ShortSessionResult("claude-code", "ok", text="VIBE_ESCALATE: 不确定怎么改，也许要授权")]
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.run(max_cycles=3)
        self.assertIn(("daemon_heal", "engineering_unresolved", "needs_decision"), self.event_kinds())
        self.assertEqual(decision_list(self.paths, RUN), [])
        self.assertEqual(self.pushed, [])

    def test_budget_exhaustion_is_recorded_without_another_session(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled(), max_sessions=0)
        daemon.run(max_cycles=3)
        self.assertEqual(self.sessions, [])
        self.assertIn(("daemon_heal", "engineering_unresolved", "budget_exhausted"), self.event_kinds())
        self.assertEqual(self.pushed, [])

    def test_token_budget_counts_session_usage(self):
        usage = {"input": 400, "output": 200, "cache_read": 5, "cache_creation": 0}
        self.session_results = [ss.ShortSessionResult("claude-code", "ok", text="x", usage=usage)]
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled(), max_tokens=500)
        daemon.run(max_cycles=4)
        self.assertEqual(len(self.sessions), 1)
        self.assertIn(("daemon_heal", "engineering_unresolved", "budget_exhausted"), self.event_kinds())

    def test_pack_escalate_tier_is_recorded_without_asking_the_product_manager(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled("escalate_to_pm"))
        daemon.run(max_cycles=2)
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.pushed, [])
        self.assertEqual(decision_list(self.paths, RUN), [])

    def test_given_up_node_gets_no_further_repair_session(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.run(max_cycles=6)
        count = len(self.sessions)
        for _ in range(6):
            daemon.tick()
        self.assertEqual(len(self.sessions), count)

    def test_changed_fingerprint_resets_the_repair_count(self):
        supervisor = FakeSupervisor(["running"])
        counter = {"n": 0}

        def run_once():
            counter["n"] += 1
            return SimpleNamespace(status="running", nodes={NODE: {"status": "running", "developer_generation": counter["n"]}},
                                   tasks={}, authorization={})
        supervisor.run_once = run_once
        daemon = self.daemon(supervisor, assess=lambda: stalled())
        daemon.run(max_cycles=5)
        # every pass sees a new fingerprint: only level-1 retries, never a session
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.pushed, [])


class CacheHealthTests(Base):
    def _result(self, cache_read):
        return ss.ShortSessionResult("claude-code", "ok", text="x", usage={
            "input": 10, "output": 1, "cache_read": cache_read, "cache_creation": 0})

    def test_three_zero_cache_reads_in_a_row_warn(self):
        daemon = self.daemon(FakeSupervisor(["running"]))
        state = daemon._heal_state()
        for _ in range(2):
            daemon._account_usage(state, self._result(0))
        self.assertNotIn("cache_health", [k[1] for k in self.event_kinds()])
        daemon._account_usage(state, self._result(0))
        self.assertIn(("daemon_heal", "cache_health", "cache_read_zero_streak"), self.event_kinds())

    def test_a_cache_hit_resets_the_streak(self):
        daemon = self.daemon(FakeSupervisor(["running"]))
        state = daemon._heal_state()
        for value in (0, 0, 7, 0, 0):
            daemon._account_usage(state, self._result(value))
        self.assertEqual(state["cache_zero_streak"], 2)
        self.assertNotIn("cache_health", [k[1] for k in self.event_kinds()])

    def test_missing_usage_neither_counts_nor_resets(self):
        daemon = self.daemon(FakeSupervisor(["running"]))
        state = daemon._heal_state()
        daemon._account_usage(state, self._result(0))
        daemon._account_usage(state, ss.ShortSessionResult("codex", "ok", text="x"))
        self.assertEqual(state["cache_zero_streak"], 1)


class StatusAndProcessTests(Base):
    def write_record(self, **overrides):
        record = {"run_id": RUN, "pid": os.getpid(), "state": "running", "reason": "",
                  "heartbeat": time.time(), "cycles": 4, "stale_seconds": 120}
        record.update(overrides)
        (run_dir(self.paths, RUN) / dm.DAEMON_FILE).write_text(json.dumps(record), encoding="utf-8")

    def test_not_started(self):
        self.assertEqual(dm.daemon_status(self.paths, RUN)["status"], "not_started")

    def test_running(self):
        self.write_record()
        self.assertEqual(dm.daemon_status(self.paths, RUN)["status"], "running")

    def test_dead_pid_is_inactive_with_reason(self):
        self.write_record(pid=2 ** 22 + 12345)
        status = dm.daemon_status(self.paths, RUN, alive=lambda pid: False)
        self.assertEqual((status["status"], status["reason"]), ("inactive", "process_not_alive"))

    def test_stale_heartbeat_is_inactive(self):
        self.write_record(heartbeat=time.time() - 3600)
        status = dm.daemon_status(self.paths, RUN)
        self.assertEqual((status["status"], status["reason"]), ("inactive", "heartbeat_stale"))

    def test_lease_taken_by_someone_else_is_inactive(self):
        self.write_record()
        lease = run_dir(self.paths, RUN) / "supervisor.lease"
        lease.write_text(json.dumps({"pid": os.getpid() + 1, "run_id": RUN, "lease_epoch": 3,
                                     "heartbeat": time.time(), "released": False}), encoding="utf-8")
        status = dm.daemon_status(self.paths, RUN)
        self.assertEqual((status["status"], status["reason"]), ("inactive", "lease_lost"))

    def test_killing_the_real_process_flips_status_to_inactive(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(process.kill)
        self.write_record(pid=process.pid)
        self.assertEqual(dm.daemon_status(self.paths, RUN)["status"], "running")
        process.kill()
        process.wait()
        status = dm.daemon_status(self.paths, RUN)
        self.assertEqual((status["status"], status["reason"]), ("inactive", "process_not_alive"))

    def test_start_reports_already_running_and_never_spawns_a_second(self):
        self.write_record()
        def spawn(*a, **k):
            raise AssertionError("must not spawn")
        self.assertEqual(dm.start_daemon(self.paths, RUN, "plan-x", spawn=spawn)["status"], "already_running")

    def test_start_spawns_detached_command_and_waits_for_running(self):
        seen = {}

        def spawn(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            self.write_record()
            return SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        status = dm.start_daemon(self.paths, RUN, "plan-x", interval=5, spawn=spawn)
        self.assertEqual(status["status"], "started")
        self.assertEqual(seen["command"][1:], ["-m", "vibe_guide", "daemon", "run", "--plan", "plan-x",
                                               "--run-id", RUN, "--interval", "5"])
        self.assertTrue(seen["kwargs"]["start_new_session"])

    def test_start_passes_model_and_provider_to_the_run_process(self):
        seen = {}

        def spawn(command, **kwargs):
            seen["command"] = command
            self.write_record()
            return SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        dm.start_daemon(self.paths, RUN, "plan-x", model="m-1", provider="codex", spawn=spawn)
        self.assertEqual(seen["command"][-4:], ["--model", "m-1", "--provider", "codex"])

    def test_stop_sends_sigterm_only_to_a_running_daemon(self):
        self.assertEqual(dm.stop_daemon(self.paths, RUN)["status"], "not_running")
        self.write_record()
        sent = []
        state = {"alive": True}

        def kill(pid, sig):
            sent.append(sig)
            state["alive"] = False
        result = dm.stop_daemon(self.paths, RUN, alive=lambda pid: state["alive"], kill=kill)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(sent, [signal.SIGTERM])

    def test_cli_status_reports_inactive_after_the_process_is_gone(self):
        self.write_record(pid=2 ** 22 + 999)
        result = run_cli(["daemon", "status", "--run-id", RUN, "--json"], Path(self.tmp.name))
        self.assertEqual(result.payload["status"], "inactive")
        self.assertNotEqual(result.exit_code, 0)

    def test_cli_requires_run_id(self):
        result = run_cli(["daemon", "status", "--json"], Path(self.tmp.name))
        self.assertEqual(result.payload["status"], "blocked_invalid")


class NotifierConfigTests(Base):
    def test_no_registered_session_still_keeps_the_message(self):
        result = dm.PmNotifier(self.paths, RUN)("hello", "escalation", NODE)
        self.assertEqual(result["status"], "no_address")
        inbox = (run_dir(self.paths, RUN) / dm.PM_INBOX_FILE).read_text(encoding="utf-8")
        self.assertIn("hello", inbox)

    def test_registered_session_without_a_channel_is_queued_not_pushed(self):
        dm.register_pm_address(self.paths, RUN, "claude-code", "pm-session")
        result = dm.PmNotifier(self.paths, RUN)("hello", "escalation")
        self.assertEqual(result["status"], "queued_inbox")

    def test_user_level_command_pushes_to_the_registered_name(self):
        dm.register_pm_address(self.paths, RUN, "codex", "pm-session")
        (Path(self.tmp.name) / "home" / "config.json").write_text(
            json.dumps({"daemon_push_command": ["send", "{target}", "{message}"]}), encoding="utf-8")
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0)
        result = dm.PmNotifier(self.paths, RUN, runner=runner)("hello", "decision")
        self.assertEqual(result["status"], "sent")
        self.assertEqual(calls, [["send", "pm-session", "hello"]])

    def test_a_project_file_cannot_name_a_push_command(self):
        dm.register_pm_address(self.paths, RUN, "codex", "pm-session")
        vibe = Path(self.tmp.name) / ".vibe"
        vibe.mkdir(exist_ok=True)
        (vibe / "config.json").write_text(json.dumps({"daemon_push_command": ["rm", "-rf", "/"]}), encoding="utf-8")
        def runner(*a, **k):
            raise AssertionError("project config must not run programs")
        self.assertEqual(dm.PmNotifier(self.paths, RUN, runner=runner)("x", "k")["status"], "queued_inbox")

    def test_config_from_project_and_validation(self):
        vibe = Path(self.tmp.name) / ".vibe"
        vibe.mkdir(exist_ok=True)
        (vibe / "config.json").write_text(json.dumps({"daemon": {"max_sessions": 2, "platform": "codex"}}), encoding="utf-8")
        config = dm.load_config(self.paths, {"interval": 5})
        self.assertEqual((config.max_sessions, config.platform, config.interval), (2, "codex", 5))
        with self.assertRaises(ValueError):
            dm.load_config(self.paths, {"platform": "nope"})
        (vibe / "config.json").write_text(json.dumps({"daemon": {"bogus": 1}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            dm.load_config(self.paths)

    def test_executable_comes_from_vibe_home_only(self):
        vibe = Path(self.tmp.name) / ".vibe"
        vibe.mkdir(exist_ok=True)
        (vibe / "config.json").write_text(json.dumps({"daemon": {"executable": "/tmp/evil"}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            dm.load_config(self.paths)
        (vibe / "config.json").write_text("{}", encoding="utf-8")
        (Path(self.tmp.name) / "home" / "config.json").write_text(
            json.dumps({"daemon": {"executable": "/usr/local/bin/claude", "extra_args": ["--x"]}}), encoding="utf-8")
        config = dm.load_config(self.paths)
        self.assertEqual((config.executable, config.extra_args), ("/usr/local/bin/claude", ["--x"]))


if __name__ == "__main__":
    unittest.main()
