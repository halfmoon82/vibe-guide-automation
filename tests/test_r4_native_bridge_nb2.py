"""Public offline NB2 regressions; audited shapes are synthetic, not App proof."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from tests import support_vibe_home as hermetic
from tests import test_r4_supervisor_responsibility as help_fixture
from tests import test_r4_recovery_claims as recovery_fixture
from vibe_guide.cli import run_cli
from vibe_guide.task_registry import load_task_binding

setUpModule, tearDownModule = hermetic.module_hooks()


class JournalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.p = help_fixture.HelpCausalityR7Tests("test_matching_A_success_resolves_A_help")
        self.p.setUp()
        self.addCleanup(self.p.doCleanups)

    def shape(self, rows, *, flag="-c"):
        # Represents native-journal-shape-audit.md; never edits a real journal.
        for row in rows:
            item = row.get("payload", {}).get("item", {})
            if item.get("type") != "CommandExecution":
                continue
            item["command"] = ["/bin/zsh", flag, item["command"]]
            item["cwd"] = Path(item["cwd"]).as_uri()
            code = item.pop("exitCode")
            item["exit_code"] = code
            item["status"] = "failed" if code else "completed"

    def prepare_result(self):
        p = self.p
        evidence = p.paths.root / "diagnosis-A.md"
        body = dict(prompt="retry exact command A", original_step=p.step_a,
            evidence_ref=evidence.name, evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())
        source = p.paths.root / "nb2-prepare.json"
        source.write_text(json.dumps(body))
        return run_cli(["supervisor-mailbox", "--run-id", p.snapshot.run_id, "--node", "n1",
            "--role", "developer", "--prepare-recovery", "--payload", str(source), "--json"], p.paths.root)

    def prepare(self):
        result = self.prepare_result()
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["status"], "claimed")
        return self.p.store.action(result.payload["action_id"])

    def reconcile(self, action):
        p = self.p
        return run_cli(["supervisor-mailbox", "--run-id", p.snapshot.run_id,
            "--reconcile-recovery", action["action_id"], "--json"], p.paths.root)

    def test_native_baseline_and_completed_nonzero_recovery_count_once(self):
        p = self.p
        self.shape(p.worker_rows, flag="-lc")
        p.flush()
        action = self.prepare()
        boundary = len(p.worker_rows)
        p.finish(action, 7)
        self.shape(p.worker_rows[boundary:])
        p.flush()
        result = self.reconcile(action)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["outcome"], "failed")
        self.assertEqual(result.payload["consecutive_failures"], 1)
        self.assertEqual(self.reconcile(action).payload["consecutive_failures"], 1)

    def test_native_zero_result_closes_only_bound_help(self):
        p = self.p
        p.help("A")
        action = p.prepare()
        boundary = len(p.worker_rows)
        p.finish(action, 0)
        self.shape(p.worker_rows[boundary:], flag="-lc")
        p.flush()
        result = self.reconcile(action)
        self.assertEqual(result.payload["outcome"], "success")
        self.assertEqual(json.loads(p.help_file.read_text())["nodes"]["n1"]["supervisor_responsibility"]["status"], "verified")

    def test_percent_encoded_writer_subdirectory_is_exact(self):
        p = self.p
        old = p.step_a["cwd"]
        p.step_a["cwd"] = str(Path(old) / "space 中文")
        for row in p.worker_rows:
            item = row.get("payload", {}).get("item", {})
            if item.get("command") == p.step_a["command"]:
                item["cwd"] = p.step_a["cwd"]
        self.shape(p.worker_rows)
        p.flush()
        action = self.prepare()
        boundary = len(p.worker_rows)
        p.step = p.step_a
        p.finish(action, 0)
        self.shape(p.worker_rows[boundary:])
        p.flush()
        self.assertIn("%", p.worker_rows[-2]["payload"]["item"]["cwd"])
        self.assertEqual(self.reconcile(action).payload["outcome"], "success")

    def baseline_case(self, case):
        p = self.p
        self.shape(p.worker_rows)
        item = p.worker_rows[3]["payload"]["item"]
        if case == "conflict": item["exitCode"] = 0
        elif case == "bool": item["exit_code"] = True
        elif case == "remote": item["cwd"] = "file://remote.example" + p.step_a["cwd"]
        elif case == "outside": item["cwd"] = (p.paths.root / "other").as_uri()
        elif case == "argv": item["command"].append("extra")
        elif case == "command": item["command"][2] += " extra"
        elif case == "duplicate": p.worker_rows.insert(4, copy.deepcopy(p.worker_rows[3]))
        elif case == "aborted": p.worker_rows[4]["payload"]["type"] = "turn_aborted"
        elif case == "source": p.worker_rows[0]["payload"]["id"] = "foreign"
        elif case == "owner":
            p.lease.takeover(p.snapshot.run_id, p.epoch, owner={"session_id": "foreign", "provider": "codex"})
        else: raise AssertionError(case)
        p.flush()
        before = list(p.store._directory("requests").glob("*.json"))
        self.assertNotEqual(self.prepare_result().exit_code, 0)
        self.assertEqual(list(p.store._directory("requests").glob("*.json")), before)

    def test_contradictory_exit_never_settles_bound_help_or_active_attempt(self):
        p = self.p
        p.help("A")
        action = p.prepare()
        p.finish(action, 0)
        p.worker_rows[-2]["payload"]["item"]["exit_code"] = 9
        p.flush()
        result = self.reconcile(action)
        self.assert_unknown(result, action)
        self.assertEqual(json.loads(p.help_file.read_text())["nodes"]["n1"]["supervisor_responsibility"]["status"], "pending")

    def assert_unknown(self, result, action):
        self.assertEqual(result.payload["status"], "unknown", result.payload)
        self.assertIsNone(result.payload["outcome"])
        self.assertEqual(result.payload["consecutive_failures"], 0)
        self.assertEqual(result.payload["active_action_id"], action["action_id"])

    def fresh_case(self, case):
        p = self.p
        action = self.prepare()
        boundary = len(p.worker_rows)
        p.finish(action, 0)
        self.shape(p.worker_rows[boundary:])
        item = p.worker_rows[-2]["payload"]["item"]
        if case == "reverse_conflict": item.update(exitCode=9, exit_code=0)
        elif case == "bool_new": item["exit_code"] = False
        elif case == "bool_old": item["exitCode"] = False
        elif case == "equal_dual": item["exitCode"] = 0
        elif case == "failed_zero": item["status"] = "failed"
        elif case == "completed_nonzero": item["exit_code"] = 9
        elif case == "running": item["status"] = "running"
        elif case == "remote_url": item["cwd"] = "file://remote.example" + p.step_a["cwd"]
        elif case == "outside": item["cwd"] = (p.paths.root / "other-writer").as_uri()
        elif case == "query": item["cwd"] += "?x=1"
        elif case == "other_command": item["command"][2] += " # not exact"
        elif case == "extra_argv": item["command"].append("extra")
        elif case == "other_shell": item["command"][0] = "/bin/bash"
        elif case == "duplicate": p.worker_rows.insert(-1, copy.deepcopy(p.worker_rows[-2]))
        elif case == "aborted": p.worker_rows[-1]["payload"]["type"] = "turn_aborted"
        elif case == "no_terminal": p.worker_rows.pop()
        elif case == "source_mismatch": p.worker_rows[0]["payload"]["id"] = "foreign"
        elif case == "two_turns":
            p.step = p.step_a
            p.add_turn("extra-turn", "retry exact command A", 0, 30)
        else: raise AssertionError(case)
        p.flush()
        result = self.reconcile(action)
        if case == "equal_dual": self.assertEqual(result.payload["outcome"], "success", result.payload)
        else: self.assert_unknown(result, action)


for _case in ("reverse_conflict", "bool_new", "bool_old", "equal_dual", "failed_zero",
              "completed_nonzero", "running", "remote_url", "outside", "query", "other_command",
              "extra_argv", "other_shell", "duplicate", "aborted", "no_terminal", "source_mismatch", "two_turns"):
    setattr(JournalBridgeTests, "test_fresh_" + _case, lambda self, case=_case: self.fresh_case(case))
for _case in ("conflict", "bool", "remote", "outside", "argv", "command", "duplicate", "aborted", "source", "owner"):
    setattr(JournalBridgeTests, "test_baseline_" + _case, lambda self, case=_case: self.baseline_case(case))


class RetirementCommandBridgeTests(unittest.TestCase):
    def test_native_failed_completed_commands_do_not_become_unresolved_side_effects(self):
        p = recovery_fixture.NativeRecoveryTerminalTests("test_public_termination_verifies_synthetic_registered_source_without_releasing_writer")
        p.setUp()
        self.addCleanup(p.doCleanups)
        shape = lambda rows: JournalBridgeTests.shape(self, rows)
        shape(p.worker_rows)
        p.flush()
        original = p.finish
        def finish(action, exit_code=1):
            boundary = len(p.worker_rows)
            original(action, exit_code)
            shape(p.worker_rows[boundary:])
            p.flush()
        p.finish = finish
        action, observation, receipt, sha = p.terminated_fixture()
        result = p.store.verify_successor_termination(action["action_id"], observation,
            owner_epoch=p.epoch, owner_session_id="supervisor-1")
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["successor_authorized"])


class ProtectedBridgeTests(unittest.TestCase):
    def setUp(self):
        self.p = recovery_fixture.ProtectedSuccessorTests("test_protected_visible_sdd_retirement_enters_normal_live_binding")
        self.p.setUp()
        self.addCleanup(self.p.doCleanups)

    def exercise(self, case, packaging):
        parent = self.p
        p = parent.probe
        runner, state, create, wait = parent.successor_probe()
        body = {"timedOut": False, "polls": [{"thread": {"id": "protected-successor",
            "hostId": "local", "status": {"type": "idle"}}, "cursor": "opaque:NB2/protected"}]}
        if case == "error": body["errors"] = [{"threadId": "protected-successor", "hostId": "local", "error": "denied"}]
        elif case == "partial": body["partial"] = True
        elif case == "page": body["nextCursor"] = "next"
        elif case == "more": body["hasMore"] = True
        elif case == "wrong_host": body["polls"][0]["thread"]["hostId"] = "foreign"
        elif case == "wrong_task": body["polls"][0]["thread"]["id"] = "foreign"
        elif case == "duplicate": body["polls"] *= 2
        elif case == "timeout": body["timedOut"] = True
        response = {"isError": case == "tool_error"}
        if packaging in {"text", "equal", "conflict"}:
            response["content"] = [{"type": "text", "text": json.dumps(body)}]
        if packaging in {"structured", "equal"}: response["structuredContent"] = body
        if packaging == "conflict": response["structuredContent"] = {"polls": []}
        source = p.paths.root / "nb2-protected.json"
        source.write_text(json.dumps(response))
        result = run_cli(["supervisor-mailbox", "--run-id", state.run_id, "--complete", wait["action_id"],
            "--request-digest", wait["request_digest"], "--native-response", "--result", str(source), "--json"], p.paths.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        raw = p.store._native_receipt_path(wait["action_id"]).read_bytes()
        state = p.monitor.tick(state.run_id, runner)
        pending = p.store.pending(state.run_id)
        good = case in {"good", "timeout"} and packaging != "conflict"
        self.assertEqual(any(a["operation"] == "locate" for a in pending), good, state.nodes["n1"].get("reason"))
        self.assertFalse(any(a["operation"] in {"visibility", "start"} for a in pending))
        self.assertEqual(load_task_binding(p.paths, "n1", "developer", run_id=state.run_id).task_id, p.binding.task_id)
        self.assertEqual(p.store._native_receipt_path(wait["action_id"]).read_bytes(), raw)
        actions = [p.store._read(path) for path in p.store._directory("requests").glob("*.json")]
        self.assertEqual(sum(a["operation"] == "create" for a in actions), 1)
        if not good: self.assertNotIn("cursor", p.store.result(wait["action_id"]))


for _packaging in ("text", "structured", "equal"):
    for _case in ("good", "error", "partial", "page", "more", "wrong_host", "wrong_task", "duplicate", "tool_error", "timeout"):
        setattr(ProtectedBridgeTests, "test_" + _packaging + "_" + _case,
            lambda self, case=_case, packaging=_packaging: self.exercise(case, packaging))
setattr(ProtectedBridgeTests, "test_conflict", lambda self: self.exercise("good", "conflict"))
