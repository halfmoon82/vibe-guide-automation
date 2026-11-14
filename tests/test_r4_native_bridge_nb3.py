"""Public baseline boundary probes; journals are synthetic offline fixtures."""
import copy
import json
import unittest
from tests import support_vibe_home as hermetic
from tests import test_r4_native_bridge_nb2 as nb2

setUpModule, tearDownModule = hermetic.module_hooks()


class BaselineBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.bridge = nb2.JournalBridgeTests("test_native_baseline_and_completed_nonzero_recovery_count_once")
        self.bridge.setUp()
        self.addCleanup(self.bridge.doCleanups)

    def exercise(self, case, *, help_only=False):
        bridge, p = self.bridge, self.bridge.p
        bridge.shape(p.worker_rows)
        start, command, terminal = [copy.deepcopy(p.worker_rows[i]) for i in (1, 3, 4)]
        if case == "aborted_complete":
            aborted = copy.deepcopy(terminal); aborted["payload"]["type"] = "turn_aborted"
            p.worker_rows.insert(4, aborted)
        elif case == "complete_aborted":
            aborted = copy.deepcopy(terminal); aborted["payload"]["type"] = "turn_aborted"
            p.worker_rows.insert(5, aborted)
        elif case == "command_after_terminal": p.worker_rows[3], p.worker_rows[4] = terminal, command
        elif case == "command_before_start": p.worker_rows[1], p.worker_rows[3] = command, start
        elif case == "duplicate_terminal": p.worker_rows.insert(5, terminal)
        elif case == "terminal_alias_duplicate":
            terminal["payload"]["type"] = "task_completed"; p.worker_rows.insert(5, terminal)
        elif case == "duplicate_start": p.worker_rows.insert(2, start)
        elif case == "missing_start": p.worker_rows.pop(1)
        elif case == "missing_terminal": p.worker_rows.pop(4)
        elif case == "cross_turn_command": p.worker_rows[3]["payload"]["turn_id"] = "failure-B"
        elif case == "foreign_thread_command": p.worker_rows[3]["payload"]["thread_id"] = "foreign"
        elif case == "foreign_thread_terminal": p.worker_rows[4]["payload"]["thread_id"] = "foreign"
        elif case == "foreign_thread_start": p.worker_rows[1]["payload"]["thread_id"] = "foreign"
        elif case == "duplicate_command": p.worker_rows.insert(4, command)
        elif case == "overlap_start":
            start["payload"]["turn_id"] = "overlap"; p.worker_rows.insert(3, start)
        elif case == "reused_turn": p.worker_rows.extend([start, command, terminal])
        elif case == "early_terminal_time": p.worker_rows[4]["payload"]["completed_at"] = 0
        elif case == "late_start_time": p.worker_rows[1]["payload"]["started_at"] = 10
        elif case == "bad_command_time": p.worker_rows[3]["payload"]["completed_at_ms"] = 0
        elif case == "latest_valid_history":
            p.step = p.step_a; p.add_turn("new-A-failure", "new A failure", 4, 10)
            bridge.shape(p.worker_rows[-4:])
        elif case == "latest_invalid_no_fallback":
            p.step = p.step_a; p.add_turn("new-A-failure", "new A failure", 4, 10)
            bridge.shape(p.worker_rows[-4:])
            p.worker_rows[-1]["payload"]["type"] = "turn_aborted"
        elif case == "valid_native_clocks":
            p.worker_rows[1]["payload"]["started_at"] = 0.001
            p.worker_rows[4]["payload"]["completed_at"] = 0.002
        elif case == "valid_alias": p.worker_rows[4]["payload"]["type"] = "task_completed"
        elif case == "legacy":
            item = p.worker_rows[3]["payload"]["item"]
            item.update(command=p.step_a["command"], cwd=p.step_a["cwd"], exitCode=item.pop("exit_code"), status="completed")
        elif case not in {"valid", "bad_ordinal", "bool_ordinal"}: raise AssertionError(case)
        p.flush()
        if case in {"bad_ordinal", "bool_ordinal"}:
            # Do not use flush, which legitimately assigns ordinals.
            rows = copy.deepcopy(p.worker_rows)
            rows[3]["ordinal"] = True if case == "bool_ordinal" else 99
            p.worker.write_text("".join(json.dumps(row) + chr(10) for row in rows))
        before = list(p.store._directory("requests").glob("*.json"))
        if help_only:
            try:
                p.help("A")
            except AssertionError:
                pass
            state = json.loads(p.help_file.read_text())["nodes"]["n1"]["supervisor_responsibility"]
            self.assertFalse(any(row.get("causal_binding") for row in state.get("attempted_actions", [])))
            self.assertEqual(list(p.store._directory("requests").glob("*.json")), before)
            return
        result = bridge.prepare_result()
        good = case in {"valid", "valid_alias", "legacy", "latest_valid_history", "valid_native_clocks"}
        if not good:
            self.assertNotEqual(result.exit_code, 0, result.payload)
            self.assertEqual(list(p.store._directory("requests").glob("*.json")), before)
            self.assertFalse(list(p.store._directory("recovery-claims").glob("*.json")))
            return
        self.assertEqual(result.exit_code, 0, result.payload)
        action = p.store.action(result.payload["action_id"])
        boundary = len(p.worker_rows)
        p.step = p.step_a; p.finish(action, 0)
        bridge.shape(p.worker_rows[boundary:])
        p.flush()
        self.assertEqual(bridge.reconcile(action).payload["outcome"], "success")
        self.assertEqual(bridge.reconcile(action).payload["outcome"], "success")


for _case in ("aborted_complete", "complete_aborted", "command_after_terminal", "command_before_start",
              "duplicate_terminal", "terminal_alias_duplicate", "duplicate_start", "missing_start",
              "missing_terminal", "cross_turn_command", "foreign_thread_command", "foreign_thread_terminal",
              "foreign_thread_start", "duplicate_command", "overlap_start", "reused_turn",
              "early_terminal_time", "late_start_time", "bad_command_time", "bad_ordinal", "bool_ordinal",
              "latest_valid_history", "latest_invalid_no_fallback", "valid_native_clocks", "valid_alias", "legacy", "valid"):
    setattr(BaselineBoundaryTests, "test_" + _case, lambda self, case=_case: self.exercise(case))
for _case in ("aborted_complete", "command_after_terminal", "duplicate_start"):
    setattr(BaselineBoundaryTests, "test_help_" + _case, lambda self, case=_case: self.exercise(case, help_only=True))
