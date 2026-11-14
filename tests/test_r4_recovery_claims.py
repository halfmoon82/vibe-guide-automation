"""One durable claim spans daemon/heartbeat retries and supervisor handoff."""
import unittest
import copy
from vibe_guide.state import load_snapshot, retirement_writer_lease
import hashlib
import threading
import json
from pathlib import Path
from unittest.mock import patch



from tests import test_monitor as fixture
from tests import support_vibe_home as hermetic
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import run_cli
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.supervisor import SupervisorLease
from vibe_guide.task_registry import load_task_binding
from vibe_guide import digests
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.monitor import Monitor
from vibe_guide.runners.provider_action import ProviderActionRunner, complete_native_mailbox_action


class Interrupted(BaseException):
    pass

setUpModule, tearDownModule = hermetic.module_hooks()


class RecoveryClaimTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.MonitorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.paths = self.fixture.paths
        monitor, authorization = self.fixture.authorized_monitor([fixture.node("n1")])
        self.monitor = monitor
        self.runner = FakeRunner()
        self.snapshot = monitor.start(authorization, self.runner)
        self.binding = load_task_binding(self.paths, "n1", "developer", run_id=self.snapshot.run_id)
        self.lease = SupervisorLease(self.paths)
        self.epoch = self.lease.takeover(self.snapshot.run_id, 0,
            owner={"session_id": "supervisor-1", "provider": "codex"})
        self.store = ProviderActionStore(self.paths)
        (self.paths.root / "failure.md").write_text("synthetic failed original step")
        rules = patch("vibe_guide.protocol_adoption.effective_responsibility",
                      return_value={"status": "effective"})
        rules.start()
        self.addCleanup(rules.stop)

    def action(self, sequence=0):
        request = {"threadId": self.binding.task_id, "hostId": self.binding.host,
            "authorization_digest": self.snapshot.authorization_digest,
            "node_contract_digest": self.snapshot.node_contract_digest,
            "recovery": {"task_id": self.binding.task_id, "owner_epoch": self.epoch,
                         "action": "resume_original", "evidence_ref": "failure.md",
                         "evidence_sha256": hashlib.sha256(b"synthetic failed original step").hexdigest()}}
        with patch("vibe_guide.adapters.task_provider.require_entry"):
            return self.store.request(operation="resume", provider=self.binding.provider,
                run_id=self.snapshot.run_id, issue_id="n1", role="developer",
                generation=self.binding.generation, native_tool="native_resume",
                request=request, sequence=sequence)

    def claim(self, action):
        return self.store.claim_recovery(action["action_id"], owner_epoch=self.epoch,
                                         owner_session_id="supervisor-1")

    def test_same_action_has_one_claim_and_no_failure_from_polling(self):
        action = self.action()
        self.assertEqual(self.claim(action)["status"], "claimed")
        for _ in range(4):
            result = self.claim(action)
            self.assertEqual(result["status"], "already_claimed")
            self.assertEqual(result["consecutive_failures"], 0)
        self.assertIsNone(self.store.result(action["action_id"]))

    def test_timeout_or_second_action_cannot_clear_an_unresolved_attempt(self):
        first = self.action()
        self.claim(first)
        second = self.action(1)
        blocked = self.claim(second)
        self.assertEqual(blocked["status"], "unknown")
        self.assertEqual(blocked["active_action_id"], first["action_id"])
        self.store.complete(first["action_id"], {"status": "unknown_timeout"})
        self.assertEqual(self.claim(second)["status"], "unknown")

    def test_send_acceptance_is_not_terminal_recovery_success(self):
        action = self.action()
        self.claim(action)
        self.store.complete(action["action_id"], {"resumed": True})
        result = self.claim(self.action(1))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["active_action_id"], action["action_id"])
        self.assertEqual(result["consecutive_failures"], 0)

    def test_unclassified_native_error_is_not_a_completed_recovery_failure(self):
        action = self.action()
        self.claim(action)
        self.store.complete(action["action_id"], {"failed": True, "reason": "capacity"})
        result = self.claim(self.action(1))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["consecutive_failures"], 0)

    def test_handoff_reconciles_inflight_action_and_refuses_old_owner(self):
        first = self.action()
        self.claim(first)
        old_epoch = self.epoch
        self.epoch = self.lease.takeover(self.snapshot.run_id, old_epoch,
            owner={"session_id": "supervisor-2", "provider": "codex"})
        with self.assertRaisesRegex(PermissionError, "owner"):
            self.store.claim_recovery(first["action_id"], owner_epoch=old_epoch,
                                      owner_session_id="supervisor-1")
        continued = self.store.claim_recovery(first["action_id"], owner_epoch=self.epoch,
                                              owner_session_id="supervisor-2")
        self.assertEqual(continued["status"], "already_claimed")
        self.assertEqual(continued["claimed_owner_epoch"], old_epoch)
        self.assertTrue(continued["reconcile_only"])

    def test_worker_report_is_not_a_completed_native_recovery(self):
        first = self.action()
        self.claim(first)
        self.store.complete(first["action_id"], {"failed": True, "reason": "self-report"}, native=False)
        result = self.claim(self.action(1))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["consecutive_failures"], 0)

    def test_parallel_callers_cannot_claim_two_attempts_for_the_same_problem(self):
        actions = [self.action(0), self.action(1)]
        barrier = threading.Barrier(2)
        results = []
        def call(action):
            barrier.wait()
            results.append(self.claim(action)["status"])
        threads = [threading.Thread(target=call, args=(action,)) for action in actions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(results), ["claimed", "unknown"])

    def test_missing_diagnostic_evidence_cannot_authorize_a_recovery(self):
        action = self.action()
        (self.paths.root / "failure.md").unlink()
        with self.assertRaisesRegex(PermissionError, "evidence"):
            self.claim(action)

    def test_public_mailbox_claim_is_bound_to_the_actual_supervisor_session(self):
        action = self.action()
        args = ["supervisor-mailbox", "--run-id", self.snapshot.run_id,
                "--claim-recovery", action["action_id"], "--json"]
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "supervisor-1"}, clear=True):
            first = run_cli(args, cwd=self.paths.root)
            second = run_cli(args, cwd=self.paths.root)
        self.assertEqual(first.exit_code, 0)
        self.assertEqual(first.payload["status"], "claimed")
        self.assertEqual(second.payload["status"], "already_claimed")
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "unregistered"}, clear=True):
            denied = run_cli(args, cwd=self.paths.root)
        self.assertNotEqual(denied.exit_code, 0)

    def test_terminal_provider_state_does_not_prove_three_recovery_failures(self):
        for state in ("failed", "stopped", "done"):
            with self.subTest(state=state):
                self.runner.session_state = lambda handle: state
                result = self.monitor.retire_dead_session("n1", self.snapshot.run_id, self.runner)
                self.assertFalse(result["retired"])
                from vibe_guide.state import load_snapshot
                after = load_snapshot(self.paths, self.snapshot.run_id)
                self.assertEqual(after.handles, self.snapshot.handles)
                self.assertEqual(after.nodes["n1"]["active_task"], self.snapshot.nodes["n1"]["active_task"])
                self.assertIsNot((after.nodes["n1"].get("retryable_action") or {}).get("successor"), True)

    def test_nonresumable_transport_error_cannot_dispatch_r4_successor(self):
        from vibe_guide.runners.claude_bg import PROVIDER_HTTP_400
        self.runner.events[("n1", "developer")] = [("failed_retryable", {
            "reason": "gateway rejected request", "reason_code": PROVIDER_HTTP_400,
            "resumable": False})]
        after = self.monitor.tick(self.snapshot.run_id, self.runner)
        self.assertEqual(len(self.runner.start_calls), 1)
        self.assertEqual(after.handles, self.snapshot.handles)
        self.assertEqual(after.nodes["n1"]["active_task"], self.snapshot.nodes["n1"]["active_task"])
        self.assertIsNot((after.nodes["n1"].get("retryable_action") or {}).get("successor"), True)


class NativeRecoveryTerminalTests(unittest.TestCase):
    """Synthetic native host logs test correlation, never real App success."""

    def setUp(self):
        from vibe_guide.task_registry import TaskBinding
        from vibe_guide.providers import CODEX_PROVIDER
        target_role = "reviewer" if "retired_reviewer" in self._testMethodName else "developer"
        class VisibleRunner(FakeRunner):
            def task_binding(self, contract, worktree, run_id, status):
                if hasattr(test_case, "protected_binding"):
                    return test_case.protected_binding(contract, worktree, run_id, status)
                return TaskBinding(provider=CODEX_PROVIDER, mode="visible",
                    topology="dual-visible", task_id=("original-worker" if contract["role"] == target_role else "completed-developer"), host="local",
                    issue_id=contract["node_id"], role=contract["role"],
                    worktree=str(worktree), branch=contract["branch"],
                    run_id=run_id, status=status, generation=contract["generation"],
                    allowlist=list(contract.get("files", [])), cursor="original-cursor")
        test_case = self
        self.fixture = fixture.MonitorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.paths = self.fixture.paths
        self.home = self.paths.root / "native-home"
        patcher = patch.dict("os.environ", {"CODEX_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        native_node = fixture.node("n1")
        native_node.contract["project_id"] = "fixture-project"
        if hasattr(self, "prepare_protected_node"):
            from vibe_guide.capability_contract import build_contract, save_contract
            save_contract(self.paths, build_contract(self.paths.root, provider="codex", host_id="local"))
            self.prepare_protected_node(native_node)
        if target_role == "reviewer":
            native_node.status = "ready"
        self.monitor, authorization = self.fixture.authorized_monitor([native_node])
        runner = VisibleRunner()
        self.initial_runner = runner
        self.snapshot = self.monitor.start(authorization, runner)
        if hasattr(self, "prepare_protected_node"):
            from vibe_guide.supervisor import register_supervisor_address
            directory = self.home / "sessions/2026/11/09"
            directory.mkdir(parents=True)
            journal = directory / "rollout-test-supervisor-1.jsonl"
            journal.write_text(json.dumps({"type": "session_meta", "payload": {"id": "supervisor-1"}}) + "\n" + json.dumps({"context_tokens": 100}) + "\n")
            register_supervisor_address(self.paths, self.snapshot.run_id,
                {"session_id": "supervisor-1", "provider": "codex", "host": "local"}, session_record=journal)
            with patch.dict("os.environ", {"CODEX_THREAD_ID": "supervisor-1"}):
                self.snapshot = self.monitor.resume(self.snapshot.run_id, runner)
        if target_role == "reviewer":
            runner.events[("n1", "developer")] = [("delivered", {"evidence": "fixture delivery"})]
            with patch("vibe_guide.protocol_adoption.effective_responsibility", return_value={"status": "effective"}):
                self.snapshot = self.monitor.tick(self.snapshot.run_id, runner)
        self.binding = load_task_binding(self.paths, "n1", target_role, run_id=self.snapshot.run_id)
        self.lease = SupervisorLease(self.paths)
        if hasattr(self, "prepare_protected_node"):
            from vibe_guide.supervisor import read_lease_epoch
            self.epoch = read_lease_epoch(self.paths, self.snapshot.run_id)
        else:
            self.epoch = self.lease.takeover(self.snapshot.run_id, 0,
                owner={"session_id": "supervisor-1", "provider": "codex"})
        self.store = ProviderActionStore(self.paths)
        rules = patch("vibe_guide.protocol_adoption.effective_responsibility",
                      return_value={"status": "effective"})
        rules.start()
        self.addCleanup(rules.stop)
        directory = self.home / "sessions/2026/11/09"
        directory.mkdir(parents=True, exist_ok=True)
        self.worker = directory / "rollout-test-original-worker.jsonl"
        self.observer = directory / "rollout-test-supervisor-1.jsonl"
        self.step = {"command": "python -m unittest original_case", "cwd": self.binding.worktree}
        self.worker_rows = [{"type": "session_meta", "payload": {"id": self.binding.task_id}}]
        self.observer_rows = [{"type": "session_meta", "payload": {"id": "supervisor-1"}}]
        self.add_turn("original-failure", "initial task", 1, 1)
        self.flush()
        (self.paths.root / "failure.md").write_text("original command failed")

    def flush(self):
        for path, rows in ((self.worker, self.worker_rows), (self.observer, self.observer_rows)):
            for ordinal, row in enumerate(rows):
                row["ordinal"] = ordinal
            path.write_text("".join(json.dumps(row) + chr(10) for row in rows))

    def add_turn(self, turn, prompt, exit_code, timestamp):
        self.worker_rows.extend([
            {"type": "event_msg", "timestamp": timestamp, "payload": {"type": "task_started", "turn_id": turn}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": prompt}]}},
            {"type": "event_msg", "payload": {"type": "item_completed",
                "thread_id": self.binding.task_id, "turn_id": turn, "started_at_ms": timestamp,
                "completed_at_ms": timestamp + 1, "item": {"id": "command-" + turn,
                    "type": "CommandExecution", **self.step, "status": "completed", "exitCode": exit_code}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}}])

    def action(self, sequence, help_digest=None):
        native = {"threadId": self.binding.task_id, "hostId": self.binding.host,
                  "prompt": "retry original step attempt-" + str(sequence)}
        request = {**native, "authorization_digest": self.snapshot.authorization_digest,
            "node_contract_digest": self.snapshot.node_contract_digest,
            "recovery": {"task_id": self.binding.task_id, "owner_epoch": self.epoch,
                "action": "resume_original", "evidence_ref": "failure.md",
                "evidence_sha256": hashlib.sha256(b"original command failed").hexdigest(),
                "original_step": self.step, "native_request": native}}
        if help_digest is not None:
            request["recovery"]["help_digest"] = help_digest
        with patch("vibe_guide.adapters.task_provider.require_entry"):
            return self.store.request(operation="resume", provider=self.binding.provider,
                run_id=self.snapshot.run_id, issue_id="n1", role=self.binding.role,
                generation=self.binding.generation, native_tool="codex_app__send_message_to_thread",
                request=request, sequence=sequence)

    def claim(self, action):
        return self.store.claim_recovery(action["action_id"], owner_epoch=self.epoch, owner_session_id="supervisor-1")

    def finish(self, action, exit_code=1):
        native = action["request"]["recovery"]["native_request"]
        call_id = "call-" + str(action["sequence"])
        meta = {"turn_id": "supervisor-turn"}
        self.observer_rows.extend([
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                "call_id": call_id, "input": "text(await tools.mcp__codex_app__send_message_to_thread(" + json.dumps(native) + "));"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": "supervisor-1",
                "turn_id": "supervisor-turn", "started_at_ms": 10, "completed_at_ms": 11,
                "item": {"id": call_id, "type": "McpToolCall", "server": "codex_app",
                    "tool": "send_message_to_thread", "arguments": native, "status": "completed",
                    "result": {"isError": False, "content": []}}}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output",
                "call_id": call_id, "output": "queued", "internal_chat_message_metadata_passthrough": meta}}])
        self.add_turn("recovery-" + str(action["sequence"]), native["prompt"], exit_code, 20)
        self.flush()

    def reconcile(self, action):
        return self.store.reconcile_recovery(action["action_id"], owner_epoch=self.epoch, owner_session_id="supervisor-1")

    def test_native_original_step_failures_count_once_and_success_resets(self):
        for sequence in range(3):
            action = self.action(sequence)
            self.assertEqual(self.claim(action)["status"], "claimed")
            self.finish(action)
            result = self.reconcile(action)
            self.assertEqual(result["outcome"], "failed")
            self.assertEqual(result["consecutive_failures"], sequence + 1)
            self.assertEqual(self.reconcile(action)["consecutive_failures"], sequence + 1)
        success = self.action(3)
        self.claim(success)
        self.finish(success, 0)
        self.assertEqual(self.reconcile(success)["consecutive_failures"], 0)

    def test_queued_or_missing_new_turn_stays_unknown(self):
        action = self.action(0)
        self.claim(action)
        self.store.complete(action["action_id"], {"resumed": True})
        result = self.reconcile(action)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["consecutive_failures"], 0)

    def test_public_successor_failure_sequence_requires_three_distinct_verified_failures(self):
        actions = []
        for sequence in range(3):
            action = self.action(sequence)
            self.claim(action)
            self.finish(action)
            self.reconcile(action)
            actions.append(action)
        result = self.store.successor_failure_sequence(actions[-1]["action_id"],
            owner_epoch=self.epoch, owner_session_id="supervisor-1")
        self.assertEqual(result["status"], "verified")
        self.assertEqual([row["action_id"] for row in result["failure_sequence"]],
                         [action["action_id"] for action in actions])
        self.assertFalse(result["successor_authorized"])
        self.assertEqual(len({row["native_call_id"] for row in result["failure_sequence"]}), 3)
        success = self.action(3)
        self.claim(success)
        self.finish(success, 0)
        self.reconcile(success)
        result = self.store.successor_failure_sequence(success["action_id"],
            owner_epoch=self.epoch, owner_session_id="supervisor-1")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["failure_sequence"], [])

    def test_public_successor_failure_sequence_rejects_duplicate_claim_evidence(self):
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        self.reconcile(action)
        path, = self.store._directory("recovery-claims").glob("*.json")
        state = self.store._read(path)
        state["attempts"] *= 3
        self.store._atomic(path, state)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.store.successor_failure_sequence(action["action_id"],
                owner_epoch=self.epoch, owner_session_id="supervisor-1")

    def test_successor_termination_rejects_caller_native_flag_without_provider_source(self):
        for sequence in range(3):
            action = self.action(sequence)
            self.claim(action)
            self.finish(action)
            self.reconcile(action)
        before = load_task_binding(self.paths, "n1", "developer",
                                   run_id=self.snapshot.run_id).to_dict()
        with self.assertRaises((PermissionError, ValueError)):
            self.store.verify_successor_termination(action["action_id"],
                {"native": True, "terminated": True, "session_id": self.binding.task_id},
                owner_epoch=self.epoch, owner_session_id="supervisor-1")
        self.assertEqual(load_task_binding(self.paths, "n1", "developer",
            run_id=self.snapshot.run_id).to_dict(), before)

    def terminated_fixture(self):
        from vibe_guide.supervisor import _record_digest
        for sequence in range(3):
            action = self.action(sequence)
            self.claim(action)
            self.finish(action)
            self.reconcile(action)
        read = {"id": self.binding.task_id, "hostId": "local",
                "status": {"type": "terminated"},
                "latest_turn": [{"id": "recovery-2", "status": "completed", "error": None}]}
        archived = {"threadId": self.binding.task_id, "archived": True}
        refs = {"read_thread": "read-terminal", "set_thread_archived": "archive-terminal"}
        for tool, args, result in (
                ("read_thread", {"threadId": self.binding.task_id}, read),
                ("set_thread_archived", {"threadId": self.binding.task_id, "archived": True}, archived)):
            self.observer_rows.extend([
                {"type": "response_item", "payload": {"type": "function_call", "name": tool,
                    "call_id": refs[tool], "arguments": json.dumps(args)}},
                {"type": "response_item", "payload": {"type": "function_call_output",
                    "call_id": refs[tool], "output": json.dumps(result)}}])
        self.flush()
        archive = self.home / "archived_sessions" / self.worker.name
        archive.parent.mkdir()
        self.worker.rename(archive)
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = self.paths.root / "termination.json"
        receipt.write_text(json.dumps({"schema": 1, "kind": "native_consumer_termination_evidence",
            "plan_id": self.snapshot.plan_id, "run_id": self.snapshot.run_id,
            "source": "Codex App public read_thread and set_thread_archived tools",
            "observer": {"session_id": "supervisor-1", "lease_epoch": self.epoch,
                "session_record": str(self.observer), "original_session_record": str(self.observer),
                "source_prefix_bytes": self.observer.stat().st_size,
                "source_prefix_sha256": sha(self.observer)},
            "consumers": [{"thread_id": self.binding.task_id, "native_observation": read,
                "native_archive_result": archived, "native_call_refs": refs}]}))
        self.store._atomic(self.paths.vibe / "runs" / self.snapshot.run_id / "supervisor-registry.json",
            {"current": {"provider": "codex", "session_id": "supervisor-1", "host": "local",
                "lease_epoch": self.epoch, "session_record_digest": _record_digest(self.observer)}, "history": []})
        observation = {"session_id": self.binding.task_id, "host": "local",
            "rollout_path": str(archive), "rollout_sha256": sha(archive), "native_calls": [],
            "native_terminal": {"archive_status": "archived", "read_thread_status": "terminated",
                "receipt_path": str(receipt), "receipt_sha256": sha(receipt),
                "source_rollout_path": str(self.observer), "source_rollout_sha256": sha(self.observer)}}
        return action, observation, receipt, sha

    def test_public_termination_verifies_synthetic_registered_source_without_releasing_writer(self):
        action, observation, receipt, sha = self.terminated_fixture()
        before = load_task_binding(self.paths, "n1", "developer", run_id=self.snapshot.run_id).to_dict()
        result = self.store.verify_successor_termination(action["action_id"], observation,
            owner_epoch=self.epoch, owner_session_id="supervisor-1")
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["successor_authorized"])
        self.assertEqual(load_task_binding(self.paths, "n1", "developer",
            run_id=self.snapshot.run_id).to_dict(), before)
        # Source authentication and terminal semantics are separate gates.
        frozen = receipt.read_bytes()
        for change in ("archive_only", "source_drift", "old_observer", "unregistered"):
            with self.subTest(change=change):
                altered = json.loads(json.dumps(observation))
                if change == "archive_only":
                    altered["native_terminal"]["read_thread_status"] = "archived"
                elif change == "source_drift":
                    receipt.write_bytes(frozen + b" ")
                elif change == "old_observer":
                    stale = json.loads(frozen)
                    stale["observer"]["lease_epoch"] = self.epoch - 1
                    receipt.write_text(json.dumps(stale))
                    altered["native_terminal"]["receipt_sha256"] = sha(receipt)
                else:
                    self.store._atomic(self.paths.vibe / "runs" / self.snapshot.run_id /
                        "supervisor-registry.json", {"current": None, "history": []})
                with self.assertRaises((PermissionError, ValueError)):
                    self.store.verify_successor_termination(action["action_id"], altered,
                        owner_epoch=self.epoch, owner_session_id="supervisor-1")
                receipt.write_bytes(frozen)

    def test_public_retirement_preserves_content_and_queues_only_one_successor(self):
        action, observation, receipt, sha = self.terminated_fixture()
        before = load_task_binding(self.paths, "n1", "developer", run_id=self.snapshot.run_id).to_dict()
        with self.assertRaises((PermissionError, ValueError)):
            self.monitor.retire_verified_successor(self.snapshot.run_id, action["action_id"],
                observation, owner_epoch=self.epoch, owner_session_id="supervisor-1",
                frozen_preservation={})
        self.assertEqual(load_task_binding(self.paths, "n1", "developer",
            run_id=self.snapshot.run_id).to_dict(), before)
        import subprocess
        root = Path(self.binding.worktree)
        root.mkdir(parents=True, exist_ok=True)
        def git(*args):
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.test", *args], check=True, capture_output=True)
        git("init", "-b", self.binding.branch)
        git("commit", "--allow-empty", "-m", "baseline")
        (root / "n1.py").write_text("preserved = True\n")
        preserved = self.monitor.successor_preservation(self.snapshot.run_id, "n1")
        result = self.monitor.retire_verified_successor(self.snapshot.run_id, action["action_id"],
            observation, owner_epoch=self.epoch, owner_session_id="supervisor-1",
            frozen_preservation=preserved)
        self.assertEqual(result["status"], "retired")
        self.assertTrue(result["successor_queued"])
        self.assertEqual((root / "n1.py").read_text(), "preserved = True\n")
        self.assertEqual(load_task_binding(self.paths, "n1", "developer",
            run_id=self.snapshot.run_id).status, "stopped")
        from vibe_guide.state import load_snapshot, load_events
        state = load_snapshot(self.paths, self.snapshot.run_id)
        self.assertIsNone(state.nodes["n1"]["active_task"])
        self.assertTrue(state.nodes["n1"]["retryable_action"]["successor"])
        replay = self.monitor.retire_verified_successor(self.snapshot.run_id, action["action_id"],
            observation, owner_epoch=self.epoch, owner_session_id="supervisor-1",
            frozen_preservation=preserved)
        self.assertEqual(replay["retirement_digest"], result["retirement_digest"])
        self.assertEqual(len([row for row in load_events(self.paths, self.snapshot.run_id)
                             if row["event"] == "verified_successor_retired"]), 1)
        count = len(load_events(self.paths, self.snapshot.run_id))
        self.monitor.retire_verified_successor(self.snapshot.run_id, action["action_id"],
            observation, owner_epoch=self.epoch, owner_session_id="supervisor-1",
            frozen_preservation=preserved)
        self.assertEqual(len(load_events(self.paths, self.snapshot.run_id)), count)

    def retirement_fixture(self):
        action, observation, receipt, sha = self.terminated_fixture()
        import subprocess
        root = Path(self.binding.worktree)
        root.mkdir(parents=True, exist_ok=True)
        for args in (() if hasattr(self, "prepare_protected_node") else (("init", "-b", self.binding.branch),
                     ("commit", "--allow-empty", "-m", "baseline"))):
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.test", *args], check=True, capture_output=True)
        (root / "tracked.py").write_text("baseline = True\n")
        for args in (() if hasattr(self, "prepare_protected_node") else (("add", "tracked.py"), ("commit", "-m", "tracked baseline"))):
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.test", *args], check=True, capture_output=True)
        (root / "tracked.py").write_text("staged = True\n")
        subprocess.run(["git", "-C", str(root), "add", "tracked.py"], check=True, capture_output=True)
        (root / "tracked.py").write_text("unstaged = True\n")
        (root / "n1.py").write_text("preserved = True\n")
        preserved = self.monitor.successor_preservation(self.snapshot.run_id, "n1")
        def retire(**overrides):
            arguments = dict(owner_epoch=self.epoch, owner_session_id="supervisor-1",
                             frozen_preservation=preserved)
            arguments.update(overrides)
            return self.monitor.retire_verified_successor(self.snapshot.run_id,
                action["action_id"], observation, **arguments)
        return action, observation, receipt, preserved, retire

    def crash_retirement(self, boundary):
        from vibe_guide import monitor as module
        from vibe_guide.state import load_events, load_snapshot
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        directory = self.paths.vibe / "runs" / self.snapshot.run_id
        intent_path = directory / "successor-retirements" / (action["action_id"] + ".json")
        if boundary in {"prepared", "committed"}:
            target, name = ProviderActionStore, "_atomic"
            original = target._atomic
            def injected(path, payload):
                original(path, payload)
                if path == intent_path and payload.get("phase") == boundary:
                    raise RuntimeError("injected crash")
        elif boundary in {"binding", "lease"}:
            target = module
            name = "save_task_binding" if boundary == "binding" else "release_writer_lease"
            original = getattr(target, name)
            def injected(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("injected crash")
        elif boundary == "event":
            target, name = self.monitor, "_record"
            original = target._record
            def injected(snapshot, event, data, *args, **kwargs):
                original(snapshot, event, data, *args, **kwargs)
                if event == "verified_successor_retired":
                    raise RuntimeError("injected crash")
        else:
            target, name = self.monitor, "_save_snapshot"
            original = target._save_snapshot
            def injected(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("injected crash")
        with patch.object(target, name, side_effect=injected):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                retire()
        phase = json.loads(intent_path.read_text())["phase"]
        if phase == "prepared":
            with patch.object(self.monitor, "_poll_active_handles") as poll, \
                 patch.object(self.monitor, "_schedule_ready") as schedule:
                with self.assertRaisesRegex(PermissionError, "dispatch blocked_unknown"):
                    self.monitor.tick(self.snapshot.run_id, FakeRunner())
                poll.assert_not_called()
                schedule.assert_not_called()
        result = retire()
        self.assertEqual(result, retire())
        self.assertEqual(json.loads(intent_path.read_text())["phase"], "committed")
        self.assertEqual(self.monitor.successor_preservation(self.snapshot.run_id, "n1"), preserved)
        events = [row for row in load_events(self.paths, self.snapshot.run_id)
                  if row["event"] == "verified_successor_retired"]
        self.assertEqual(len(events), 1)
        state = load_snapshot(self.paths, self.snapshot.run_id)
        self.assertIsNone(state.nodes["n1"]["active_task"])
        self.assertEqual(state.nodes["n1"]["retryable_action"]["retirement_digest"],
                         result["retirement_digest"])

    def test_retirement_crash_after_prepared(self):
        self.crash_retirement("prepared")

    def test_retirement_crash_after_binding(self):
        self.crash_retirement("binding")

    def test_retirement_crash_after_lease_release(self):
        self.crash_retirement("lease")

    def test_retirement_crash_after_event(self):
        self.crash_retirement("event")

    def test_retirement_crash_after_snapshot(self):
        self.crash_retirement("snapshot")

    def test_retirement_crash_after_commit(self):
        self.crash_retirement("committed")

    def test_retirement_replay_rejects_old_owner_and_changed_evidence(self):
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        with self.assertRaisesRegex(PermissionError, "owner"):
            retire(owner_session_id="old-owner")
        frozen_receipt = receipt.read_bytes()
        receipt.write_bytes(frozen_receipt + b" ")
        with self.assertRaises((PermissionError, ValueError)):
            retire()
        receipt.write_bytes(frozen_receipt)
        root = Path(self.binding.worktree)
        (root / "n1.py").write_text("changed = True\n")
        with self.assertRaisesRegex(PermissionError, "preservation changed"):
            retire()
        (root / "n1.py").write_text("preserved = True\n")
        path = self.paths.vibe / "runs" / self.snapshot.run_id / "successor-retirements" / (action["action_id"] + ".json")
        saved = json.loads(path.read_text())
        saved["intent"]["binding"]["generation"] += 1
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(PermissionError, "intent mismatch"):
            retire()

    def test_prepared_retirement_refuses_foreign_lease_and_changed_binding(self):
        from vibe_guide import digests
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        directory = self.paths.vibe / "runs" / self.snapshot.run_id
        path = directory / "successor-retirements" / (action["action_id"] + ".json")
        original = ProviderActionStore._atomic
        def crash(target, payload):
            original(target, payload)
            if target == path:
                raise RuntimeError("prepared")
        with patch.object(ProviderActionStore, "_atomic", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "prepared"):
                retire()
        saved = json.loads(path.read_text())
        lease_path = self.paths.vibe / "leases" / saved["intent"]["writer_lease"]["filename"]
        frozen_lease = lease_path.read_bytes()
        foreign = json.loads(frozen_lease)
        foreign["run_id"] = "another-run"
        lease_path.write_text(json.dumps(foreign))
        with self.assertRaisesRegex(PermissionError, "lease"):
            retire()
        lease_path.write_bytes(frozen_lease)
        saved["intent"]["binding"]["generation"] += 1
        saved["intent"]["digest"] = digests.canonical_json_digest(
            {key: value for key, value in saved["intent"].items() if key != "digest"})
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(PermissionError, "binding changed"):
            retire()

    def test_retirement_replay_refuses_owner_after_takeover(self):
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "supervisor-2", "provider": "codex"})
        with self.assertRaisesRegex(PermissionError, "owner"):
            retire()

    def test_committed_retirement_recovers_older_snapshot_without_duplicate_events(self):
        from vibe_guide.state import load_events, load_snapshot
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        state_path = self.paths.vibe / "runs" / self.snapshot.run_id / "state.json"
        older = state_path.read_bytes()
        first = retire()
        state_path.write_bytes(older)
        restored = retire()
        self.assertEqual(first, restored)
        state = load_snapshot(self.paths, self.snapshot.run_id)
        self.assertIsNone(state.nodes["n1"]["active_task"])
        self.assertEqual(state.nodes["n1"]["retryable_action"]["retirement_digest"],
                         first["retirement_digest"])
        self.assertEqual(len([row for row in load_events(self.paths, self.snapshot.run_id)
            if row["event"] == "verified_successor_retired"]), 1)

    def test_retired_developer_creates_one_visible_successor_through_normal_scheduler(self):
        self.assert_successor_chain()

    def test_retired_reviewer_creates_one_visible_successor_through_normal_scheduler(self):
        from vibe_guide.state import retirement_writer_lease
        writer = retirement_writer_lease(self.paths, "n1", self.snapshot.nodes["n1"]["worktree"], self.snapshot.run_id)
        self.assertEqual(writer["record"]["role"], "reviewer")
        self.assertEqual(writer["record"]["generation"], self.binding.generation)
        self.assert_successor_chain()

    def test_developer_to_reviewer_lease_release_conflict_blocks_without_reviewer_start(self):
        self.check_delivery_release_conflict()

    def test_two_review_rework_roundtrips_hold_correct_lease_before_every_start(self):
        from vibe_guide.state import retirement_writer_lease
        fake = self.initial_runner
        fake.events[("n1", "developer")] = [("delivered", {"evidence": "delivery"})] * 3
        fake.events[("n1", "reviewer")] = [
            ("review_finding", {"finding": "fix one", "in_contract": True}),
            ("review_finding", {"finding": "fix two", "in_contract": True}),
            ("accepted", {"evidence": "independent clearance"})]
        original = fake.start
        seen = []
        def checked(contract, worktree):
            lease = retirement_writer_lease(self.paths, "n1", ".worktrees/n1", self.snapshot.run_id)
            self.assertIsNotNone(lease)
            self.assertEqual(lease["record"]["role"], contract["role"])
            self.assertEqual(lease["record"]["generation"], contract["generation"])
            seen.append((contract["role"], contract["generation"], contract["task_id"]))
            return original(contract, worktree)
        with patch.object(fake, "start", side_effect=checked):
            for _ in range(6):
                state = self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "accepted", state.nodes["n1"].get("reason"))
        self.assertEqual([(role, generation) for role, generation, _ in seen],
            [("reviewer", 1), ("developer", 2), ("reviewer", 2), ("developer", 3), ("reviewer", 3)])
        self.assertEqual(len({task for role, _, task in seen if role == "developer"}), 1)
        self.assertEqual(len({task for role, _, task in seen if role == "reviewer"}), 1)

    def check_delivery_release_conflict(self):
        from vibe_guide.state import retirement_writer_lease
        # Rebuild a clean developer delivery fixture, then force the CAS
        # release to report a conflict. The public tick must park before any
        # reviewer provider start and retain the developer lease.
        fake = self.initial_runner
        fake.events[("n1", "developer")] = [("delivered", {"evidence": "delivery"})]
        with patch("vibe_guide.monitor.release_writer_lease", return_value=False):
            state = self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(len(fake.start_calls), 1)
        lease = retirement_writer_lease(self.paths, "n1", state.nodes["n1"]["worktree"], state.run_id)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["record"].get("role"), "developer")

    def begin_rework_review(self):
        fake = self.initial_runner
        fake.events[("n1", "developer")] = [("delivered", {"evidence": "preserved delivery"})]
        state = self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "review")
        fake.events[("n1", "reviewer")] = [("review_finding", {"finding": "ordinary P1", "in_contract": True})]
        return fake

    def assert_rework_resumes(self, fake):
        from vibe_guide.state import retirement_writer_lease
        original = fake.start
        def checked(contract, worktree):
            lease = retirement_writer_lease(self.paths, "n1", ".worktrees/n1", self.snapshot.run_id)
            self.assertEqual((lease["record"]["role"], lease["record"]["generation"]),
                             (contract["role"], contract["generation"]))
            return original(contract, worktree)
        with patch.object(fake, "start", side_effect=checked):
            state = self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "rework", state.nodes["n1"].get("reason"))
        self.assertEqual(len(fake.start_calls), 3)
        self.assertEqual(fake.start_calls[-1]["task_id"], self.binding.task_id)
        bound = load_task_binding(self.paths, "n1", "developer", run_id=self.snapshot.run_id)
        self.assertEqual(bound.cursor, "original-cursor")
        self.assertEqual(bound.generation, 2)

    def test_reverse_handoff_release_conflict_parks_then_resumes(self):
        fake = self.begin_rework_review()
        with patch("vibe_guide.monitor.release_writer_lease", return_value=False):
            state = self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertIn("release CAS", state.nodes["n1"]["reason"])
        self.assertEqual(len(fake.start_calls), 2)
        self.assert_rework_resumes(fake)

    def test_reverse_handoff_acquire_conflict_parks_then_resumes(self):
        fake = self.begin_rework_review()
        with patch("vibe_guide.monitor.acquire_writer_lease", return_value=False):
            state = self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertIn("acquire failed", state.nodes["n1"]["reason"])
        self.assertEqual(len(fake.start_calls), 2)
        self.assert_rework_resumes(fake)

    def check_reverse_crash(self, boundary):
        from vibe_guide import monitor as module
        fake = self.begin_rework_review()
        hit = []
        def crash():
            hit.append(boundary)
            raise RuntimeError("reverse boundary crash")
        if boundary in {"prepared", "committed"}:
            original = ProviderActionStore._atomic
            def interrupted(path, value):
                result = original(path, value)
                if path.parent.name == "writer-handoffs" and value.get("phase") == boundary:
                    crash()
                return result
            target = patch.object(ProviderActionStore, "_atomic", side_effect=interrupted)
        elif boundary in {"release", "acquire"}:
            name = boundary + "_writer_lease"
            original = getattr(module, name)
            def interrupted(*args, **kwargs):
                result = original(*args, **kwargs)
                crash()
                return result
            target = patch.object(module, name, side_effect=interrupted)
        elif boundary == "event":
            original = self.monitor._record
            def interrupted(snapshot, event, data, *args, **kwargs):
                result = original(snapshot, event, data, *args, **kwargs)
                if event == "writer_role_handed_off":
                    crash()
                return result
            target = patch.object(self.monitor, "_record", side_effect=interrupted)
        else:
            original = self.monitor._save_snapshot
            def interrupted(snapshot):
                result = original(snapshot)
                if snapshot.nodes["n1"].get("start_intent", {}).get("role") == "developer":
                    crash()
                return result
            target = patch.object(self.monitor, "_save_snapshot", side_effect=interrupted)
        with target:
            with self.assertRaisesRegex(RuntimeError, "reverse boundary crash"):
                self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(hit, [boundary])
        self.assertEqual(len(fake.start_calls), 2)
        self.assert_rework_resumes(fake)

    def test_reverse_handoff_prepared_crash_replays(self):
        self.check_reverse_crash("prepared")

    def test_reverse_handoff_release_crash_replays(self):
        self.check_reverse_crash("release")

    def test_reverse_handoff_acquire_crash_replays(self):
        self.check_reverse_crash("acquire")

    def test_reverse_handoff_event_crash_replays(self):
        self.check_reverse_crash("event")

    def test_reverse_handoff_committed_crash_replays(self):
        self.check_reverse_crash("committed")

    def test_reverse_handoff_snapshot_before_start_crash_replays(self):
        self.check_reverse_crash("snapshot")

    def check_reverse_evidence_change(self, change):
        fake = self.begin_rework_review()
        with patch("vibe_guide.monitor.release_writer_lease", return_value=False):
            self.monitor.tick(self.snapshot.run_id, fake)
        directory = self.paths.vibe / "runs" / self.snapshot.run_id / "writer-handoffs"
        path = next(directory.glob("n1-reviewer-1-developer-2.json"))
        saved = json.loads(path.read_text())
        if change == "owner":
            self.lease.takeover(self.snapshot.run_id, self.epoch,
                owner={"session_id": "supervisor-2", "provider": "codex"})
        else:
            intent = saved["intent"]
            if change == "tamper":
                intent["digest"] = "0" * 64
            else:
                if change in {"authorization_digest", "node_contract_digest", "role"}:
                    intent[change] = "changed"
                elif change == "generation":
                    intent["reviewer"]["generation"] += 1
                from vibe_guide import digests
                intent["digest"] = digests.canonical_json_digest(
                    {key: value for key, value in intent.items() if key != "digest"})
            path.write_text(json.dumps(saved))
        try:
            state = self.monitor.tick(self.snapshot.run_id, fake)
        except (PermissionError, ValueError):
            pass
        else:
            self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(len(fake.start_calls), 2)

    def test_reverse_handoff_old_owner_refused(self):
        self.check_reverse_evidence_change("owner")

    def test_reverse_handoff_source_generation_drift_refused(self):
        self.check_reverse_evidence_change("generation")

    def test_reverse_handoff_role_drift_refused(self):
        self.check_reverse_evidence_change("role")

    def test_reverse_handoff_authorization_drift_refused(self):
        self.check_reverse_evidence_change("authorization_digest")

    def test_reverse_handoff_contract_drift_refused(self):
        self.check_reverse_evidence_change("node_contract_digest")

    def test_reverse_handoff_tampered_digest_refused(self):
        self.check_reverse_evidence_change("tamper")

    def test_reverse_handoff_provider_start_crash_does_not_repeat_start(self):
        fake = self.begin_rework_review()
        original = fake.start
        class ProcessInterrupted(BaseException):
            pass
        def interrupted(contract, worktree):
            original(contract, worktree)
            raise ProcessInterrupted()
        with patch.object(fake, "start", side_effect=interrupted):
            with self.assertRaises(ProcessInterrupted):
                self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(len(fake.start_calls), 3)
        state = self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertIn("no provider-confirmed result", state.nodes["n1"]["reason"])
        self.assertEqual(len(fake.start_calls), 3)

    def test_reverse_handoff_heartbeat_refresh_does_not_change_owner(self):
        fake = self.begin_rework_review()
        with patch("vibe_guide.monitor.release_writer_lease", return_value=False):
            self.monitor.tick(self.snapshot.run_id, fake)
        record = json.loads(self.lease.path(self.snapshot.run_id).read_text())
        record["heartbeat"] += 1
        self.lease.path(self.snapshot.run_id).write_text(json.dumps(record))
        self.assert_rework_resumes(fake)

    def check_same_role_start_retry(self, role, conflict=None):
        from vibe_guide.state import retirement_writer_lease
        fake = self.initial_runner
        if role == "developer":
            self.begin_rework_review()
        else:
            fake.events[("n1", "developer")] = [("delivered", {"evidence": "delivery"})]
        original = fake.start
        attempts = []
        def retry_once(contract, worktree):
            attempts.append((contract["role"], contract["generation"], contract["task_id"]))
            if len(attempts) == 1 or conflict:
                return {"kind": "retryable", "reason": "temporary capacity"}
            return original(contract, worktree)
        with patch.object(fake, "start", side_effect=retry_once):
            self.monitor.tick(self.snapshot.run_id, fake)
            if conflict == "owner":
                self.lease.takeover(self.snapshot.run_id, self.epoch, owner={"session_id": "new-owner", "provider": "codex"})
            elif conflict == "foreign":
                from vibe_guide.state import release_writer_lease, acquire_writer_lease
                lease = retirement_writer_lease(self.paths, "n1", ".worktrees/n1", self.snapshot.run_id)
                self.assertTrue(release_writer_lease(self.paths, "n1", ".worktrees/n1", self.snapshot.run_id, expected=lease))
                self.assertTrue(acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "foreign-run", "f" * 64, role, attempts[0][1]))
            state = self.monitor.tick(self.snapshot.run_id, fake)
        if conflict:
            self.assertEqual(len(attempts), 2)
            self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
            if conflict == "foreign":
                held = retirement_writer_lease(self.paths, "n1", ".worktrees/n1", "foreign-run")
                self.assertEqual(held["record"]["run_id"], "foreign-run")
            return
        self.assertEqual(len(attempts), 2, state.nodes["n1"].get("reason"))
        self.assertEqual(attempts[0], attempts[1])
        lease = retirement_writer_lease(self.paths, "n1", ".worktrees/n1", state.run_id)
        self.assertEqual((lease["record"]["role"], lease["record"]["generation"]), attempts[-1][:2])
        self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(len(fake.start_calls), 3 if role == "developer" else 2)

    def test_developer_same_role_retry_preserves_generation_lease(self):
        self.check_same_role_start_retry("developer")

    def test_reviewer_same_role_retry_preserves_generation_lease(self):
        self.check_same_role_start_retry("reviewer")

    def test_developer_same_role_retry_old_owner_refused(self):
        self.check_same_role_start_retry("developer", "owner")

    def test_reviewer_same_role_retry_old_owner_refused(self):
        self.check_same_role_start_retry("reviewer", "owner")

    def test_developer_same_role_retry_foreign_lease_retained(self):
        self.check_same_role_start_retry("developer", "foreign")

    def test_reviewer_same_role_retry_foreign_lease_retained(self):
        self.check_same_role_start_retry("reviewer", "foreign")

    def mailbox_start_interruption(self, retry=False, role="developer"):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        fake = self.begin_rework_review()
        if role == "reviewer":
            self.monitor.tick(self.snapshot.run_id, fake)
            fake.events[("n1", "developer")] = [("delivered", {"evidence": "rework delivery"})]
        runner = ProviderActionRunner(self.paths, self.binding.provider, "codex-app-visible")
        original = runner.start
        handles = []
        class ProcessInterrupted(BaseException):
            pass
        def interrupted(contract, worktree):
            if retry and not handles:
                handles.append(None)
                return {"kind": "retryable", "reason": "temporary capacity"}
            handles.append(original(contract, worktree))
            raise ProcessInterrupted()
        with patch.object(fake, "start", side_effect=interrupted):
            with self.assertRaises(ProcessInterrupted):
                self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(len(handles), 2 if retry else 1)
        actions = [a for a in self.store.pending(self.snapshot.run_id) if a["operation"] == "resume"]
        self.assertEqual(len(actions), 1)
        return runner, handles[-1], actions[0]

    def test_original_prepared_replay_must_not_restart_existing_action(self):
        captured = []
        original = ProviderActionStore._atomic
        def capture(path, value):
            result = original(path, value)
            if path.parent.name == "start-dispatches" and value.get("phase") == "prepared":
                captured.append((path, path.read_bytes()))
            return result
        with patch.object(ProviderActionStore, "_atomic", side_effect=capture):
            runner, handle, action = self.mailbox_start_interruption()
        self.assertTrue(captured)
        path, original_bytes = captured[-1]
        path.write_bytes(original_bytes)
        with patch.object(runner, "start", wraps=runner.start) as start:
            self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(start.call_count, 0)
        self.assertTrue(runner._handle_path(handle.run_id).is_file())

    def test_retry_then_start_crash_reclaims_completed_action(self):
        self.check_mailbox_recovery_entry("retry")

    def test_reviewer_retry_then_start_crash_reclaims_completed_action(self):
        from vibe_guide.runners.provider_action import complete_native_mailbox_action
        runner, handle, action = self.mailbox_start_interruption(retry=True, role="reviewer")
        self.assertEqual(action["role"], "reviewer")
        complete_native_mailbox_action(self.paths, self.snapshot.run_id, action["action_id"],
            {"structuredContent": {"resumed": True}, "isError": False}, request_digest=action["request_digest"])
        with patch.object(runner, "start", side_effect=AssertionError("duplicate start")):
            state = self.monitor.resume(self.snapshot.run_id, runner)
        self.assertEqual(state.handles.get("n1"), handle.run_id)
        waits = [a for a in self.store.pending(state.run_id) if a["operation"] == "wait"]
        self.assertEqual(len(waits), 1)
        self.store.complete(waits[0]["action_id"], {"status": "complete", "event": "accepted",
            "evidence": "independent clearance", "cursor": "reviewer-recovered"})
        state = self.monitor.tick(state.run_id, runner)
        self.assertEqual(state.nodes["n1"]["status"], "accepted", state.nodes["n1"].get("reason"))
        self.assertEqual(load_task_binding(self.paths, "n1", "reviewer", run_id=state.run_id).cursor, "reviewer-recovered")

    def test_nb1_native_polls_reach_normal_consumer_without_business_completion(self):
        runner, handle, resume = self.mailbox_start_interruption()
        complete_native_mailbox_action(self.paths, self.snapshot.run_id, resume['action_id'],
            {'structuredContent': {'resumed': True}}, request_digest=resume['request_digest'])
        state = self.monitor.resume(self.snapshot.run_id, runner)
        wait, = [a for a in self.store.pending(state.run_id) if a['operation'] == 'wait']
        target, = wait['request']['targets']
        response = {'content': [{'type': 'text', 'text': json.dumps({'timedOut': False,
            'polls': [{'schemaVersion': 1, 'cursor': 'opaque:NB1/position',
                'thread': {'id': target['threadId'], 'hostId': target['hostId'], 'status': {'type': 'idle'}},
                'latestTurn': {'id': 'native-turn', 'status': 'completed', 'error': None},
                'latestAssistantMessage': {'text': 'fixture worker says done, not acceptance'}}]})}], 'isError': False}
        source = self.paths.root / 'nb1-native-wait.json'
        source.write_text(json.dumps(response))
        result = run_cli(['supervisor-mailbox', '--run-id', state.run_id, '--complete', wait['action_id'],
            '--request-digest', wait['request_digest'], '--native-response', '--result', str(source), '--json'], self.paths.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        state = self.monitor.tick(state.run_id, runner)
        binding = load_task_binding(self.paths, 'n1', 'developer', run_id=state.run_id)
        self.assertEqual(binding.cursor, 'opaque:NB1/position')
        self.assertNotIn(state.nodes['n1']['status'], ('accepted', 'complete', 'delivered'))
        self.assertFalse(runner.store._read(runner._handle_path(handle.run_id))['terminal_confirmed'])

    def test_sending_write_failure_leaves_no_replayable_prepared_start(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        fake = self.begin_rework_review()
        runner = ProviderActionRunner(self.paths, self.binding.provider, "codex-app-visible")
        original = ProviderActionStore._atomic
        class Interrupted(BaseException):
            pass
        def interrupted(path, value):
            if path.parent.name == "start-dispatches" and path.suffix == ".json" and value.get("phase") == "sending":
                raise Interrupted()
            return original(path, value)
        with patch.object(ProviderActionStore, "_atomic", side_effect=interrupted):
            with self.assertRaises(Interrupted):
                self.monitor.tick(self.snapshot.run_id, fake)
        with patch.object(runner, "start", wraps=runner.start) as start:
            state = self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(start.call_count, 0)
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(self.store.pending(state.run_id), [])

    def test_takeover_during_handle_claim_no_new_wait(self):
        from vibe_guide.runners.provider_action import complete_native_mailbox_action
        runner, handle, action = self.mailbox_start_interruption()
        complete_native_mailbox_action(self.paths, self.snapshot.run_id, action["action_id"],
            {"structuredContent": {"resumed": True}, "isError": False}, request_digest=action["request_digest"])
        original = runner.recover_start
        taken = []
        def takeover(*args, **kwargs):
            result = original(*args, **kwargs)
            taken.append(self.lease.takeover(self.snapshot.run_id, self.epoch,
                owner={"session_id": "next-owner", "provider": "codex"}))
            return result
        with patch.object(runner, "recover_start", side_effect=takeover):
            self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(taken, [self.epoch + 1])
        self.assertEqual([a for a in self.store.pending(self.snapshot.run_id) if a["operation"] == "wait"], [])

    def test_mailbox_start_crash_reclaims_original_handle_and_polls(self):
        self.check_mailbox_recovery_entry("tick")

    def test_mailbox_start_crash_public_resume_reclaims_original_handle(self):
        self.check_mailbox_recovery_entry("resume")

    def test_mailbox_start_crash_public_repair_reclaims_original_handle(self):
        self.check_mailbox_recovery_entry("repair")

    def test_mailbox_start_crash_timeout_keeps_original_handle(self):
        self.check_mailbox_recovery_entry("timeout")

    def test_mailbox_start_confirmation_crash_replays_original_handle(self):
        self.check_mailbox_recovery_entry("crash")

    def check_mailbox_recovery_entry(self, entry):
        from vibe_guide.runners.provider_action import complete_native_mailbox_action
        runner, handle, action = self.mailbox_start_interruption(retry=entry == "retry")
        complete_native_mailbox_action(self.paths, self.snapshot.run_id, action["action_id"],
            {"structuredContent": {"resumed": True}, "isError": False},
            request_digest=action["request_digest"])
        with patch.object(runner, "start", side_effect=AssertionError("duplicate start")):
            if entry == "crash":
                class Interrupted(BaseException):
                    pass
                original = self.monitor._record
                def interrupted(snapshot, event, data, *args, **kwargs):
                    result = original(snapshot, event, data, *args, **kwargs)
                    if event == "start_confirmed":
                        raise Interrupted()
                    return result
                with patch.object(self.monitor, "_record", side_effect=interrupted):
                    with self.assertRaises(Interrupted):
                        self.monitor.tick(self.snapshot.run_id, runner)
                state = self.monitor.tick(self.snapshot.run_id, runner)
            elif entry == "repair":
                repaired = self.monitor.repair_node("n1", self.snapshot.run_id)
                self.assertEqual(repaired.get("handle_id"), handle.run_id)
                state = self.monitor.tick(self.snapshot.run_id, runner)
            else:
                state = getattr(self.monitor, "tick" if entry in {"timeout", "retry"} else entry)(self.snapshot.run_id, runner)
        self.assertEqual(state.handles.get("n1"), handle.run_id)
        waits = [a for a in self.store.pending(state.run_id) if a["operation"] == "wait"]
        self.assertEqual(len(waits), 1)
        self.assertNotEqual(state.nodes["n1"]["status"], "accepted")
        self.assertEqual(waits[0]["request"]["targets"][0]["threadId"], self.binding.task_id)
        if entry == "timeout":
            self.store.complete(waits[0]["action_id"], {"status": "timeout"})
            state = self.monitor.tick(state.run_id, runner)
            self.assertEqual(state.handles.get("n1"), handle.run_id)
            waits = [a for a in self.store.pending(state.run_id) if a["operation"] == "wait"]
            self.assertEqual(len(waits), 1)
        self.store.complete(waits[0]["action_id"], {"status": "complete", "event": "delivered",
            "evidence": "recovered task delivery", "cursor": "recovered-cursor"})
        with patch.object(runner, "start", side_effect=AssertionError("duplicate start")):
            state = self.monitor.tick(state.run_id, runner)
        events = __import__("vibe_guide.state", fromlist=["load_events"]).load_events(self.paths, state.run_id)
        self.assertTrue(any(row["event"] == "delivered" and row["data"].get("generation") == 2 for row in events))
        self.assertEqual(load_task_binding(self.paths, "n1", "developer", run_id=state.run_id).cursor, "recovered-cursor")

    def check_mailbox_recovery_refused(self, cause):
        from vibe_guide.runners.provider_action import complete_native_mailbox_action
        runner, handle, action = self.mailbox_start_interruption()
        if cause != "pending":
            complete_native_mailbox_action(self.paths, self.snapshot.run_id, action["action_id"],
                {"structuredContent": {"resumed": True}, "isError": False}, request_digest=action["request_digest"])
        if cause == "owner":
            self.lease.takeover(self.snapshot.run_id, self.epoch, owner={"session_id": "new-owner", "provider": "codex"})
        elif cause == "C1":
            self.monitor.revoke_business_acceptance(self.snapshot.run_id, "n1", reason="source revoked", evidence_ref="failure.md")
        elif cause == "metadata":
            path = runner._handle_path(handle.run_id)
            row = json.loads(path.read_text())
            row["task_id"] = "foreign-task"
            path.write_text(json.dumps(row))
        with patch.object(runner, "start", side_effect=AssertionError("duplicate start")):
            state = self.monitor.tick(self.snapshot.run_id, runner)
        self.assertNotIn("n1", state.handles)
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual([a for a in self.store.pending(state.run_id) if a["operation"] == "wait"], [])

    def test_mailbox_start_pending_does_not_reissue(self):
        self.check_mailbox_recovery_refused("pending")

    def test_mailbox_start_old_owner_does_not_advance(self):
        self.check_mailbox_recovery_refused("owner")

    def test_mailbox_start_C1_does_not_advance(self):
        self.check_mailbox_recovery_refused("C1")

    def test_mailbox_start_conflicting_metadata_does_not_advance(self):
        self.check_mailbox_recovery_refused("metadata")

    def test_committed_handoff_survives_signed_scope_reauthorization(self):
        self.check_signed_scope_reauthorization()

    def test_signed_scope_reauthorization_event_crash_recovers(self):
        self.check_signed_scope_reauthorization("event")

    def test_signed_scope_reauthorization_event_old_owner_refused(self):
        self.check_signed_scope_reauthorization("owner")

    def test_signed_scope_reauthorization_snapshot_crash_recovers(self):
        self.check_signed_scope_reauthorization("snapshot")

    def test_event_before_snapshot_recovers_prepared_start(self):
        self.check_signed_scope_reauthorization("before-snapshot")

    def test_same_contract_transition_rejects_old_owner(self):
        self.check_signed_scope_reauthorization("same-contract-owner")

    def test_missing_owner_proof_cannot_bypass_takeover(self):
        self.check_signed_scope_reauthorization("missing-proof")

    def check_signed_scope_reauthorization(self, boundary=None):
        from vibe_guide.authorization import authorize, build_authorization_card
        from vibe_guide.monitor import Monitor
        from vibe_guide.state import retirement_writer_lease
        fake = self.begin_rework_review()
        self.monitor.tick(self.snapshot.run_id, fake)
        directory = self.paths.vibe / "runs" / self.snapshot.run_id / "writer-handoffs"
        history = {p.name: p.read_bytes() for p in directory.glob("*.json")}
        changed = fixture.node("n1")
        changed.contract["project_id"] = "fixture-project"
        if boundary != "same-contract-owner":
            changed.contract["files"].append("tests/test_more.py")
        card = build_authorization_card(self.monitor.plan, [changed], self.fixture.capabilities)
        if boundary == "same-contract-owner":
            card = build_authorization_card(self.monitor.plan, [changed], self.fixture.capabilities, active_pair_limit=2)
        record = authorize(card, "AUTHORIZE")
        renewed = Monitor(self.paths, self.monitor.plan, [changed])
        original_start = fake.start
        seen = []
        def checked(contract, worktree):
            lease = retirement_writer_lease(self.paths, "n1", ".worktrees/n1", self.snapshot.run_id)
            self.assertEqual((lease["record"]["role"], lease["record"]["generation"]),
                             (contract["role"], contract["generation"]))
            self.assertEqual(contract["authorization_digest"], record.digest)
            seen.append((contract["role"], contract["task_id"]))
            return original_start(contract, worktree)
        start_patch = patch.object(fake, "start", side_effect=checked)
        start_patch.start()
        self.addCleanup(start_patch.stop)
        if boundary:
            class Interrupted(BaseException):
                pass
            if boundary in {"event", "owner", "same-contract-owner", "missing-proof"}:
                original = renewed._record
                def interrupted(snapshot, event, data, *args, **kwargs):
                    result = original(snapshot, event, data, *args, **kwargs)
                    if event == "authorization_reauthorized":
                        raise Interrupted()
                    return result
                target = patch.object(renewed, "_record", side_effect=interrupted)
            else:
                original = renewed._save_snapshot
                def interrupted(snapshot):
                    if boundary == "before-snapshot" and snapshot.authorization_digest == record.digest:
                        raise Interrupted()
                    result = original(snapshot)
                    if snapshot.authorization_digest == record.digest:
                        raise Interrupted()
                    return result
                target = patch.object(renewed, "_save_snapshot", side_effect=interrupted)
            with target:
                with self.assertRaises(Interrupted):
                    renewed.reauthorize(self.snapshot.run_id, record, fake, "executable_contract_changed")
            if boundary in {"owner", "same-contract-owner", "missing-proof"}:
                if boundary == "missing-proof":
                    from vibe_guide.state import load_events
                    from vibe_guide import digests
                    events = load_events(self.paths, self.snapshot.run_id)
                    row = events[-1]
                    self.assertEqual(row["event"], "authorization_reauthorized")
                    row["data"].pop("proof")
                    # Preserve signed authorizations; only the ordinary log hash changes.
                    hash_key = "hash" if "hash" in row else "event_digest"
                    row[hash_key] = digests.canonical_json_digest({k: v for k, v in row.items() if k != hash_key})
                    (directory.parent / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
                self.lease.takeover(self.snapshot.run_id, self.epoch, owner={"session_id": "new-owner", "provider": "codex"})
                count = len(fake.start_calls)
                with self.assertRaises(PermissionError):
                    renewed.tick(self.snapshot.run_id, fake)
                self.assertEqual(len(fake.start_calls), count)
                return
            state = renewed.tick(self.snapshot.run_id, fake)
        else:
            state = renewed.reauthorize(self.snapshot.run_id, record, fake, "executable_contract_changed")
        self.assertEqual(state.authorization_digest, record.digest)
        state = renewed.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.run_id, self.snapshot.run_id)
        fake.events[("n1", "developer")] = [("delivered", {"evidence": "new scope delivery"})]
        state = renewed.tick(state.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "review", state.nodes["n1"].get("reason"))
        fake.events[("n1", "reviewer")] = [("review_finding", {"finding": "new scope P1", "in_contract": True})]
        state = renewed.tick(state.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "rework", state.nodes["n1"].get("reason"))
        fake.events[("n1", "developer")] = [("delivered", {"evidence": "new scope rework"})]
        state = renewed.tick(state.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "review", state.nodes["n1"].get("reason"))
        fake.events[("n1", "reviewer")] = [("accepted", {"evidence": "new scope clearance"})]
        state = renewed.tick(state.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "accepted", state.nodes["n1"].get("reason"))
        for role in ("developer", "reviewer"):
            self.assertEqual(len({task for current_role, task in seen if current_role == role}), 1)
        for name, content in history.items():
            self.assertEqual((directory / name).read_bytes(), content)

    def test_developer_to_reviewer_handoff_replay_keeps_one_reviewer_lease(self):
        from vibe_guide.state import retirement_writer_lease
        fake = self.initial_runner
        fake.events[("n1", "developer")] = [("delivered", {"evidence": "delivery"})]
        original = self.monitor._record
        hit = {"done": False}
        def crash(snapshot, event, data, *args, **kwargs):
            original(snapshot, event, data, *args, **kwargs)
            if event == "writer_role_handed_off" and not hit["done"]:
                hit["done"] = True
                raise RuntimeError("handoff crash")
        with patch.object(self.monitor, "_record", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "handoff crash"):
                self.monitor.tick(self.snapshot.run_id, fake)
        # Replaying the same public tick consumes durable handoff state and
        # starts one reviewer with exactly one reviewer lease.
        state = self.monitor.tick(self.snapshot.run_id, fake)
        lease = retirement_writer_lease(self.paths, "n1", state.nodes["n1"]["worktree"], state.run_id)
        self.assertEqual(lease["record"].get("role"), "reviewer")
        self.assertEqual(len([call for call in fake.start_calls if call.get("role") == "reviewer"]), 1)

    def test_delivery_handoff_acquire_failure_resumes_without_second_lease(self):
        from vibe_guide.state import retirement_writer_lease
        fake = self.initial_runner
        fake.events[("n1", "developer")] = [("delivered", {"evidence": "delivery"})]
        with patch("vibe_guide.monitor.acquire_writer_lease", return_value=False):
            state = self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(len(fake.start_calls), 1)
        self.assertIsNone(retirement_writer_lease(self.paths, "n1",
            state.nodes["n1"]["worktree"], state.run_id))
        state = self.monitor.tick(self.snapshot.run_id, fake)
        lease = retirement_writer_lease(self.paths, "n1", state.nodes["n1"]["worktree"], state.run_id)
        self.assertIsNotNone(lease, {"state": state.nodes["n1"], "starts": fake.start_calls})
        self.assertEqual(lease["record"]["role"], "reviewer")
        self.assertEqual(len(fake.start_calls), 2)

    def test_delivery_handoff_crash_after_release_resumes_without_second_lease(self):
        from vibe_guide import monitor as module
        from vibe_guide.state import retirement_writer_lease
        fake = self.initial_runner
        fake.events[("n1", "developer")] = [("delivered", {"evidence": "delivery"})]
        original = module.release_writer_lease
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("after release")
        with patch.object(module, "release_writer_lease", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "after release"):
                self.monitor.tick(self.snapshot.run_id, fake)
        self.assertEqual(len(fake.start_calls), 1)
        state = self.monitor.tick(self.snapshot.run_id, fake)
        lease = retirement_writer_lease(self.paths, "n1", state.nodes["n1"]["worktree"], state.run_id)
        self.assertEqual(lease["record"]["role"], "reviewer")
        self.assertEqual(len(fake.start_calls), 2)

    def assert_successor_chain(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        runner = ProviderActionRunner(self.paths, self.binding.provider, "codex-app-visible")
        state = self.monitor.tick(self.snapshot.run_id, runner)
        creates = [item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "create"]
        self.assertEqual(len(creates), 1, state.nodes["n1"].get("reason"))
        from vibe_guide.runners.provider_action import complete_native_mailbox_action
        for operation, payload in (("create", {"threadId": "successor-worker", "hostId": "local"}),
                                   ("locate", {"located": True}),
                                   ("visibility", {"visible": True, "direct_enter": True})):
            pending = [item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == operation]
            self.assertEqual(len(pending), 1, state.nodes["n1"].get("reason"))
            complete_native_mailbox_action(self.paths, self.snapshot.run_id, pending[0]["action_id"],
                {"structuredContent": payload, "isError": False}, request_digest=pending[0]["request_digest"])
            with patch.object(self.monitor, "_mark_blocked_unknown", wraps=self.monitor._mark_blocked_unknown) as blocked:
                state = self.monitor.tick(self.snapshot.run_id, runner)
            causes = [str(call.kwargs.get("cause")) for call in blocked.call_args_list if call.kwargs.get("cause")]
        bound = load_task_binding(self.paths, "n1", self.binding.role, run_id=self.snapshot.run_id)
        self.assertEqual(bound.task_id, "successor-worker", causes or state.nodes["n1"].get("reason"))
        self.assertEqual(bound.generation, self.binding.generation + 1)
        self.assertEqual(bound.successor_of, self.binding.task_id)
        self.assertEqual(bound.worktree, self.binding.worktree)
        self.assertEqual(bound.branch, self.binding.branch)
        self.assertEqual(bound.provider, self.binding.provider)
        self.assertEqual(bound.allowlist, self.binding.allowlist)
        self.assertEqual(bound.topology, self.binding.topology)
        self.assertTrue(bound.visible)
        from vibe_guide.state import load_events
        archive = json.loads((self.paths.vibe / "runs" / self.snapshot.run_id / "tasks.json").read_text())
        self.assertIn(self.binding.task_id, json.dumps(archive))
        self.assertIn("original-cursor", json.dumps(archive))
        for operation in ("create", "locate", "visibility"):
            rows = [self.store.action(path.stem) for path in self.store._directory("requests").glob("*.json")]
            item, = [row for row in rows if row["operation"] == operation]
            self.assertTrue(self.store._native_receipt_path(item["action_id"]).is_file())
        self.assertEqual(self.monitor.successor_preservation(self.snapshot.run_id, "n1"), preserved)
        with self.assertRaises((PermissionError, ValueError)):
            retire()
        for _ in range(2):
            state = self.monitor.tick(self.snapshot.run_id, runner)
        with self.assertRaises(ValueError):
            self.store.record_worker_delivery(self.snapshot.run_id, "n1", self.binding.role,
                {"event": "delivered", "delivery_evidence": {"completion_marker": "old",
                 "delivery_path": "old.md", "thread_status": "completed"}}, self.binding.generation)
        waits = [item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "wait"]
        self.assertEqual(len(waits), 1)
        complete_native_mailbox_action(self.paths, self.snapshot.run_id, waits[0]["action_id"],
            {"structuredContent": {"status": "timeout", "cursor": "new-successor-cursor"}},
            request_digest=waits[0]["request_digest"])
        state = self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(load_task_binding(self.paths, "n1", self.binding.role, run_id=self.snapshot.run_id).cursor,
                         "new-successor-cursor")
        creates = [self.store.action(path.stem) for path in self.store._directory("requests").glob("*.json")]
        self.assertEqual(len([item for item in creates if item["operation"] == "create"]), 1)

    def test_retirement_dispatch_rechecks_changed_source(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        receipt.write_bytes(receipt.read_bytes() + b" ")
        state = self.monitor.tick(self.snapshot.run_id, ProviderActionRunner(
            self.paths, self.binding.provider, "codex-app-visible"))
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual([item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "create"], [])

    def test_retirement_dispatch_rechecks_changed_owner(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        self.lease.takeover(self.snapshot.run_id, self.epoch, owner={"session_id": "supervisor-2", "provider": "codex"})
        state = self.monitor.tick(self.snapshot.run_id, ProviderActionRunner(
            self.paths, self.binding.provider, "codex-app-visible"))
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertIn("owner changed", state.nodes["n1"]["reason"])
        self.assertEqual([item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "create"], [])

    def test_retirement_dispatch_rechecks_changed_preservation(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        (Path(self.binding.worktree) / "n1.py").write_text("new evidence\n")
        state = self.monitor.tick(self.snapshot.run_id, ProviderActionRunner(
            self.paths, self.binding.provider, "codex-app-visible"))
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual([item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "create"], [])

    def test_successor_setup_pending_reconciles_original_action_without_second_create(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner, complete_native_mailbox_action
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        runner = ProviderActionRunner(self.paths, self.binding.provider, "codex-app-visible")
        self.monitor.tick(self.snapshot.run_id, runner)
        create, = [item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "create"]
        complete_native_mailbox_action(self.paths, self.snapshot.run_id, create["action_id"],
            {"structuredContent": {"status": "pending", "clientThreadId": "setup-only"}},
            request_digest=create["request_digest"])
        for _ in range(3):
            state = self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(load_task_binding(self.paths, "n1", self.binding.role, run_id=self.snapshot.run_id).task_id, self.binding.task_id)
        self.assertIsNone(state.nodes["n1"]["active_task"])
        creates = [self.store.action(path.stem) for path in self.store._directory("requests").glob("*.json")]
        self.assertEqual(len([item for item in creates if item["operation"] == "create"]), 1)

    def test_public_cli_retires_using_current_owner_and_rejects_foreign_caller(self):
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        payload_path = self.paths.root / "retire-input.json"
        payload_path.write_text(json.dumps({"observation": observation, "frozen_preservation": preserved}))
        args = ["supervisor-mailbox", "--project-root", str(self.paths.root),
                "--run-id", self.snapshot.run_id, "--retire-successor", action["action_id"],
                "--payload", str(payload_path), "--json"]
        with patch("vibe_guide.cli._load_plan", return_value=(None, self.monitor.plan,
                list(self.monitor.nodes.values()), None)), \
             patch("vibe_guide.cli._observed_topology_rulings", return_value={}):
            with patch("vibe_guide.supervisor.caller_session_ids", return_value=("foreign",)):
                result = run_cli(args, self.paths.root)
                self.assertNotEqual(result.exit_code, 0)
            with patch("vibe_guide.supervisor.caller_session_ids", return_value=("supervisor-1",)):
                result = run_cli(args, self.paths.root)
                self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(load_task_binding(self.paths, "n1", self.binding.role,
            run_id=self.snapshot.run_id).status, "stopped")

    def test_prepared_retirement_takeover_requires_fresh_owner_observation(self):
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        original = ProviderActionStore._atomic
        def crash(path, payload):
            original(path, payload)
            if path.parent.name == "successor-retirements":
                raise RuntimeError("prepared")
        with patch.object(ProviderActionStore, "_atomic", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "prepared"):
                retire()
        epoch = self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "supervisor-2", "provider": "codex"})
        with self.assertRaisesRegex(PermissionError, "owner"):
            retire()
        with self.assertRaisesRegex(PermissionError, "intent mismatch"):
            retire(owner_epoch=epoch, owner_session_id="supervisor-2")
        with self.assertRaisesRegex(PermissionError, "dispatch blocked_unknown"):
            self.monitor.tick(self.snapshot.run_id, FakeRunner())

    def test_retirement_business_invalidation_blocks_replacement(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        self.monitor.revoke_business_acceptance(self.snapshot.run_id, "n1",
            reason="source business evidence revoked", evidence_ref="failure.md")
        state = self.monitor.tick(self.snapshot.run_id, ProviderActionRunner(
            self.paths, self.binding.provider, "codex-app-visible"))
        self.assertEqual([item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "create"], [])
        self.assertEqual(state.nodes["n1"]["business_barrier"]["status"], "invalidated")

    def test_retirement_contract_change_cannot_reuse_old_eligibility(self):
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        self.monitor.nodes["n1"].contract["files"] = ["other.py"]
        with self.assertRaises((ValueError, PermissionError)):
            retire()

    def test_prepared_retirement_contract_change_cannot_reauthorize_old_transaction(self):
        from vibe_guide.authorization import authorize, build_authorization_card
        from vibe_guide.monitor import Monitor
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        original = ProviderActionStore._atomic
        def crash(path, payload):
            original(path, payload)
            if path.parent.name == "successor-retirements":
                raise RuntimeError("prepared")
        with patch.object(ProviderActionStore, "_atomic", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "prepared"):
                retire()
        changed = fixture.node("n1")
        changed.contract["project_id"] = "fixture-project"
        changed.contract["acceptance_example"] = "changed contract"
        record = authorize(build_authorization_card(self.monitor.plan, [changed], self.fixture.capabilities), "AUTHORIZE")
        renewed = Monitor(self.paths, self.monitor.plan, [changed])
        with self.assertRaises((PermissionError, ValueError)):
            renewed.reauthorize(self.snapshot.run_id, record, FakeRunner(), "executable_contract_changed")
        with self.assertRaises((PermissionError, ValueError)):
            renewed.retire_verified_successor(self.snapshot.run_id, action["action_id"], observation,
                owner_epoch=self.epoch, owner_session_id="supervisor-1", frozen_preservation=preserved)
        self.assertEqual([item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "create"], [])

    def test_successor_conflicting_native_create_cannot_bind(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner, complete_native_mailbox_action
        action, observation, receipt, preserved, retire = self.retirement_fixture()
        retire()
        runner = ProviderActionRunner(self.paths, self.binding.provider, "codex-app-visible")
        self.monitor.tick(self.snapshot.run_id, runner)
        create, = [item for item in self.store.pending(self.snapshot.run_id) if item["operation"] == "create"]
        complete_native_mailbox_action(self.paths, self.snapshot.run_id, create["action_id"],
            {"structuredContent": {"threadId": "one", "hostId": "local"},
             "content": [{"type": "text", "text": json.dumps({"threadId": "two", "hostId": "local"})}]},
            request_digest=create["request_digest"])
        self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(load_task_binding(self.paths, "n1", self.binding.role, run_id=self.snapshot.run_id).task_id, self.binding.task_id)

    def test_different_command_and_business_findings_do_not_count(self):
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        item = self.worker_rows[-2]["payload"]["item"]
        item["command"] = "echo unrelated review finding"
        self.flush()
        self.assertEqual(self.reconcile(action)["status"], "unknown")
        self.assertEqual(self.claim(self.action(1))["consecutive_failures"], 0)

    def test_missing_host_send_or_changed_claim_prefix_stays_unknown(self):
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        self.observer_rows[2]["payload"]["item"]["arguments"]["threadId"] = "another-task"
        self.flush()
        self.assertEqual(self.reconcile(action)["status"], "unknown")
        self.observer_rows[0]["payload"]["id"] = "foreign-observer"
        self.flush()
        self.assertEqual(self.reconcile(action)["status"], "unknown")

    def test_changed_persisted_terminal_source_invalidates_count(self):
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        self.assertEqual(self.reconcile(action)["consecutive_failures"], 1)
        self.worker_rows[-2]["payload"]["item"]["exitCode"] = 0
        self.flush()
        self.assertEqual(self.claim(self.action(1))["consecutive_failures"], 0)
        self.assertEqual(self.claim(self.action(1))["status"], "unknown")

    def test_claim_without_failed_original_command_cannot_create_terminal_count(self):
        self.worker_rows[-2]["payload"]["item"]["exitCode"] = 0
        self.flush()
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        result = self.reconcile(action)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["consecutive_failures"], 0)

    def test_duplicate_native_send_is_unknown_not_one_completed_attempt(self):
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        duplicate = json.loads(json.dumps(self.observer_rows[1:]))
        for row in duplicate:
            payload = row["payload"]
            if "call_id" in payload:
                payload["call_id"] += "-duplicate"
            if "item" in payload:
                payload["item"]["id"] += "-duplicate"
        self.observer_rows.extend(duplicate)
        self.flush()
        self.assertEqual(self.reconcile(action)["status"], "unknown")

    def test_reconcile_unclaimed_action_does_not_claim_permission_to_send(self):
        action = self.action(0)
        self.assertEqual(self.reconcile(action)["status"], "unknown")
        self.assertEqual(self.claim(action)["status"], "claimed")

    def test_recovery_command_cannot_escape_the_original_writer_root(self):
        self.step["cwd"] = str(self.paths.root / "foreign-writer")
        self.worker_rows[-2]["payload"]["item"]["cwd"] = self.step["cwd"]
        self.flush()
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        self.assertEqual(self.reconcile(action)["status"], "unknown")

    def test_verified_original_step_result_closes_only_matching_supervisor_help(self):
        from vibe_guide.supervisor import record_supervisor_help, pending_supervisor_responsibilities
        from vibe_guide.state import run_dir
        path = run_dir(self.paths, self.snapshot.run_id) / "heal-state.json"
        path.write_text(json.dumps({"nodes": {"n1": {"supervisor_responsibility": {
            "status": "pending", "node_id": "n1", "role": "developer",
            "authorization_digest": self.snapshot.authorization_digest,
            "node_contract_digest": self.snapshot.node_contract_digest}}}}))
        help_record = {"diagnosis": "original command failed", "action": "retry original command",
            "next_action": "inspect command result", "verification_condition": "original command exits zero",
            "evidence_ref": "failure.md", "evidence_sha256": hashlib.sha256(b"original command failed").hexdigest(),
            "original_step": self.step}
        helped = record_supervisor_help(self.paths, self.snapshot.run_id, "n1", help_record,
            owner_epoch=self.epoch, owner_session_id="supervisor-1")
        action = self.action(0, helped["help_digest"])
        self.claim(action)
        self.finish(action)
        self.reconcile(action)
        pending = pending_supervisor_responsibilities(self.paths, self.snapshot.run_id)
        self.assertEqual(len(pending), 1)
        attempt = pending[0]["attempted_actions"][-1]
        self.assertEqual(attempt["help_digest"], helped["help_digest"])
        self.assertEqual(attempt["status"], "verified_ineffective")
        help_record["diagnosis"] = "first advice ineffective; inspect supported invocation then retry"
        help_record["action"] = "apply supported invocation to original command"
        helped = record_supervisor_help(self.paths, self.snapshot.run_id, "n1", help_record,
            owner_epoch=self.epoch, owner_session_id="supervisor-1")
        action = self.action(1, helped["help_digest"])
        self.claim(action)
        self.finish(action, 0)
        self.reconcile(action)
        self.assertEqual(pending_supervisor_responsibilities(self.paths, self.snapshot.run_id), [])
        responsibility = json.loads(path.read_text())["nodes"]["n1"]["supervisor_responsibility"]
        self.assertEqual(responsibility["status"], "verified")
        self.assertEqual(len(responsibility["attempted_actions"]), 2)

    def test_public_prepare_uses_original_task_and_claims_exact_native_request(self):
        payload = self.paths.root / "recovery-input.json"
        payload.write_text(json.dumps({"prompt": "retry original command via public entry",
            "original_step": self.step, "evidence_ref": "failure.md",
            "evidence_sha256": hashlib.sha256(b"original command failed").hexdigest()}))
        args = ["supervisor-mailbox", "--run-id", self.snapshot.run_id, "--node", "n1",
                "--role", "developer", "--prepare-recovery", "--payload", str(payload), "--json"]
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "supervisor-1"}, clear=False):
            first = run_cli(args, self.paths.root)
            second = run_cli(args, self.paths.root)
        self.assertEqual(first.exit_code, 0, first.payload)
        self.assertEqual(first.payload["status"], "claimed")
        self.assertEqual(second.payload["status"], "already_claimed")
        self.assertEqual(first.payload["action_id"], second.payload["action_id"])
        self.assertEqual(first.payload["native_args"]["threadId"], self.binding.task_id)
        self.assertEqual(first.payload["native_tool"], "codex_app__send_message_to_thread")
        action = self.store.action(first.payload["action_id"])
        self.finish(action, 0)
        self.assertEqual(self.reconcile(action)["outcome"], "success")

    def test_public_prepare_rejects_changed_writer_before_mailbox_write(self):
        original = self.binding.to_dict()
        payload = {"prompt": "retry original", "original_step": self.step,
            "evidence_ref": "failure.md",
            "evidence_sha256": hashlib.sha256(b"original command failed").hexdigest()}
        for key, value in (("branch", "wrong-branch"), ("generation", self.binding.generation + 1),
                           ("task_id", "different-worker")):
            with self.subTest(changed=key):
                from vibe_guide.task_registry import TaskBinding
                changed = dict(original, **{key: value})
                if key == "task_id":
                    changed.update(threadId=value, platform_task_id=value)
                binding = TaskBinding.from_dict(changed)
                # Registry fixtures model a stale run/binding mismatch.
                with patch("vibe_guide.task_registry.load_task_binding", return_value=binding):
                    with self.assertRaises(PermissionError):
                        self.store.prepare_recovery(self.snapshot.run_id, "n1", "developer", payload,
                            owner_epoch=self.epoch, owner_session_id="supervisor-1")
                self.assertEqual(list(self.store._directory("recovery-claims").glob("*.json")), [])

    def test_blocked_original_task_help_prepare_and_recheck_public_chain(self):
        from vibe_guide.state import run_dir
        runner = FakeRunner(events={("n1", "developer"): [("blocked_engineering", {
            "status": "blocked", "implementation_complete": False,
            "failed_step": "original command failed", "missing_conditions": ["supported syntax"]})]})
        handle = self.snapshot.handles["n1"]
        runner._nodes_by_handle[handle] = "n1"
        runner._roles_by_handle[handle] = "developer"
        runner._claims_by_handle[handle] = {"node_id": "n1", **self.snapshot.nodes["n1"]["active_task"]}
        blocked = self.monitor.tick(self.snapshot.run_id, runner)
        self.assertIsNone(blocked.nodes["n1"]["active_task"])
        state_path = run_dir(self.paths, self.snapshot.run_id) / "heal-state.json"
        state_path.write_text(json.dumps({"nodes": {"n1": {"supervisor_responsibility": {
            "status": "pending", "node_id": "n1", "role": "developer",
            "authorization_digest": self.snapshot.authorization_digest,
            "node_contract_digest": self.snapshot.node_contract_digest}}}}))
        digest = hashlib.sha256(b"original command failed").hexdigest()
        help_path = self.paths.root / "supervisor-help.json"
        help_path.write_text(json.dumps({"diagnosis": "unsupported syntax", "action": "use supported syntax",
            "next_action": "retry original command", "verification_condition": "original command exits zero",
            "evidence_ref": "failure.md", "evidence_sha256": digest, "original_step": self.step}))
        recovery_path = self.paths.root / "recovery-input.json"
        recovery_path.write_text(json.dumps({"prompt": "retry original command", "original_step": self.step,
            "evidence_ref": "failure.md", "evidence_sha256": digest}))
        base = ["supervisor-mailbox", "--run-id", self.snapshot.run_id, "--node", "n1", "--json"]
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "supervisor-1"}):
            help_result = run_cli(base + ["--record-help", "--payload", str(help_path)], self.paths.root)
            self.assertEqual(help_result.exit_code, 0, help_result.payload)
            recovery_input = json.loads(recovery_path.read_text())
            recovery_input["help_digest"] = help_result.payload["help_digest"]
            recovery_path.write_text(json.dumps(recovery_input))
            prepared = run_cli(base + ["--prepare-recovery", "--role", "developer",
                "--payload", str(recovery_path)], self.paths.root)
            self.assertEqual(prepared.exit_code, 0, prepared.payload)
            self.assertEqual(prepared.payload["status"], "claimed")
            self.assertEqual(prepared.payload["native_args"]["threadId"], self.binding.task_id)
            action = self.store.action(prepared.payload["action_id"])
            self.finish(action, 0)
            checked = run_cli(["supervisor-mailbox", "--run-id", self.snapshot.run_id,
                "--reconcile-recovery", action["action_id"], "--json"], self.paths.root)
        self.assertEqual(checked.exit_code, 0, checked.payload)
        self.assertEqual(checked.payload["outcome"], "success")
        responsibility = json.loads(state_path.read_text())["nodes"]["n1"]["supervisor_responsibility"]
        self.assertEqual(responsibility["status"], "verified")
        self.assertEqual(responsibility["attempted_actions"][-1]["help_digest"], help_result.payload["help_digest"])
        # Recovery verification is not business acceptance or a new dispatch.
        self.assertEqual(len(runner.start_calls), 0)
        from vibe_guide.state import verify_run_evidence
        self.assertEqual(verify_run_evidence(self.paths, self.snapshot.run_id).nodes["n1"]["status"], "blocked_unknown")

    def test_prepare_carries_existing_complex_engine_gate(self):
        from vibe_guide.state import save_snapshot
        self.snapshot.execution_engine = "vibeguide_monitor"
        self.snapshot.engine_mode = "dag"
        self.snapshot.engine_evidence_ref = "missing-engine-attestation"
        save_snapshot(self.paths, self.snapshot)
        payload = {"prompt": "retry original", "original_step": self.step,
            "evidence_ref": "failure.md",
            "evidence_sha256": hashlib.sha256(b"original command failed").hexdigest()}
        with self.assertRaisesRegex(PermissionError, "complex_monitor_required"):
            self.store.prepare_recovery(self.snapshot.run_id, "n1", "developer", payload,
                owner_epoch=self.epoch, owner_session_id="supervisor-1")
        self.assertEqual(list(self.store._directory("recovery-claims").glob("*.json")), [])

    def test_disqualified_reviewer_is_not_recovered_to_accumulate_failures(self):
        from types import SimpleNamespace
        node = dict(self.snapshot.nodes["n1"], reviewer_identity="reviewer-task",
                    review_generation=1, reviewer_replacement_required=True,
                    active_task=None, status="rework")
        snapshot = SimpleNamespace(run_id=self.snapshot.run_id, nodes={"n1": node})
        binding = SimpleNamespace(issue_id="n1", role="reviewer", run_id=self.snapshot.run_id,
            task_id="reviewer-task", generation=1, worktree=node["worktree"],
            branch=node["branch"], status="blocked_unknown")
        with self.assertRaises(PermissionError):
            self.store._require_recovery_binding(snapshot, binding)

    def test_worker_command_before_native_send_is_not_correlated_terminal(self):
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        self.worker_rows[-2]["payload"]["started_at_ms"] = 8
        self.worker_rows[-2]["payload"]["completed_at_ms"] = 9
        self.flush()
        self.assertEqual(self.reconcile(action)["status"], "unknown")

    def test_changed_journal_between_parse_and_prefix_read_is_unknown(self):
        from vibe_guide.plan_amendment import _native_session_record
        action = self.action(0)
        self.claim(action)
        self.finish(action, 0)
        changed = False
        def read_then_change(session, archived):
            nonlocal changed
            path, rows = _native_session_record(session, archived)
            if session == self.binding.task_id and not changed:
                changed = True
                raw = path.read_text().replace('"exitCode": 0', '"exitCode": 1')
                path.write_text(raw)
            return path, rows
        with patch("vibe_guide.plan_amendment._native_session_record", side_effect=read_then_change):
            result = self.reconcile(action)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["outcome"])
        self.assertFalse((self.store._directory("recovery-terminals") / (action["action_id"] + ".json")).exists())

    def test_public_reconcile_rejects_old_owner_and_retains_claim_across_shift(self):
        action = self.action(0)
        self.claim(action)
        self.finish(action)
        args = ["supervisor-mailbox", "--run-id", self.snapshot.run_id,
                "--reconcile-recovery", action["action_id"], "--json"]
        epoch = self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "supervisor-2", "provider": "codex"})
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "supervisor-1"}, clear=False):
            denied = run_cli(args, self.paths.root)
        self.assertNotEqual(denied.exit_code, 0)
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "supervisor-2"}, clear=False):
            result = run_cli(args, self.paths.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["outcome"], "failed")
        self.assertEqual(result.payload["claimed_owner_epoch"], self.epoch)
        self.assertTrue(result.payload["reconcile_only"])


class AuthorityFenceTests(NativeRecoveryTerminalTests):
    def finish_resume(self, action):
        complete_native_mailbox_action(self.paths, self.snapshot.run_id,
            action["action_id"],
            {"structuredContent": {"resumed": True}, "isError": False},
            request_digest=action["request_digest"])

    def pending_transition(self, scope):
        runner = self.begin_rework_review()
        self.monitor.tick(self.snapshot.run_id, runner)
        nodes = copy.deepcopy(list(self.monitor.nodes.values()))
        if scope:
            nodes[0].contract["files"].append("tests/reviewer-scope.py")
        record = authorize(build_authorization_card(self.monitor.plan, nodes,
            self.fixture.capabilities, active_pair_limit=2), "AUTHORIZE")
        renewed = Monitor(self.paths, self.monitor.plan, nodes)
        original = renewed._record
        def interrupt(snapshot, event, data, *args, **kwargs):
            value = original(snapshot, event, data, *args, **kwargs)
            if event == "authorization_reauthorized":
                raise Interrupted()
            return value
        with patch.object(renewed, "_record", side_effect=interrupt):
            with self.assertRaises(Interrupted):
                renewed.reauthorize(self.snapshot.run_id, record, runner,
                    "executable_contract_changed")
        return renewed, record, runner

    def test_new_r4_owner_missing_is_not_legacy(self):
        runner, handle, action = self.mailbox_start_interruption()
        self.finish_resume(action)
        snapshot = self.monitor.tick(self.snapshot.run_id, runner)
        wait, = [a for a in self.store.pending(snapshot.run_id) if a["operation"] == "wait"]
        self.store.complete(wait["action_id"], {"status": "timeout"})
        path = runner._handle_path(handle.run_id)
        metadata = json.loads(path.read_text())
        self.assertTrue(metadata["intent_digest"])
        self.assertIn("start_owner", metadata)
        metadata.pop("start_owner")
        path.write_text(json.dumps(metadata))
        self.lease.takeover(snapshot.run_id, self.epoch,
            owner={"session_id": "next-owner", "provider": "codex"})
        self.monitor.tick(snapshot.run_id, runner)
        self.assertEqual(self.store.pending(snapshot.run_id), [],
            "intent-bound R4 handle lost start_owner and bypassed owner fence")

    def test_new_r4_both_fields_missing_is_not_legacy(self):
        runner, handle, action = self.mailbox_start_interruption()
        self.finish_resume(action)
        snapshot = self.monitor.tick(self.snapshot.run_id, runner)
        wait, = [a for a in self.store.pending(snapshot.run_id) if a["operation"] == "wait"]
        self.store.complete(wait["action_id"], {"status": "timeout"})
        path = runner._handle_path(handle.run_id)
        metadata = json.loads(path.read_text())
        self.assertTrue(metadata.pop("intent_digest"))
        self.assertTrue(metadata.pop("start_owner"))
        path.write_text(json.dumps(metadata))
        self.lease.takeover(snapshot.run_id, self.epoch,
            owner={"session_id": "next-owner", "provider": "codex"})
        self.monitor.tick(snapshot.run_id, runner)
        self.assertEqual(self.store.pending(snapshot.run_id), [],
            "R4 metadata field deletion downgraded real intent-bound handle to legacy")

    def test_new_sending_takeover_prevents_resume(self):
        runner = self.begin_rework_review()
        real = ProviderActionRunner(self.paths, self.binding.provider, "codex-app-visible")
        atomic = ProviderActionStore._atomic
        epochs = []
        def raced(path, value):
            result = atomic(path, value)
            if path.parent.name == "start-dispatches" and path.suffix == ".sending" and not epochs:
                epochs.append(self.lease.takeover(self.snapshot.run_id, self.epoch,
                    owner={"session_id": "next-owner", "provider": "codex"}))
            return result
        with patch.object(ProviderActionStore, "_atomic", side_effect=raced), \
             patch.object(runner, "start", side_effect=real.start) as start:
            self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(start.call_count, 0, "old tick dispatched after successful takeover at sending boundary")
        self.assertEqual(self.store.pending(self.snapshot.run_id), [])

    def test_new_transition_check_takeover_no_apply(self):
        renewed, record, runner = self.pending_transition(False)
        validate = renewed._validate_pending_transition_owner
        epochs = []
        def raced(*args):
            value = validate(*args)
            epochs.append(self.lease.takeover(self.snapshot.run_id, self.epoch,
                owner={"session_id": "next-owner", "provider": "codex"}))
            return value
        count = len(runner.start_calls)
        with patch.object(renewed, "_validate_pending_transition_owner", side_effect=raced):
            try:
                renewed.tick(self.snapshot.run_id, runner)
            except PermissionError:
                pass
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(len(runner.start_calls), count,
            "owner validation released its lock before transition apply and dispatch")

    def test_scope_transition_check_takeover_no_apply(self):
        renewed, record, runner = self.pending_transition(True)
        validate = renewed._validate_pending_transition_owner
        epochs = []
        before = len(runner.start_calls)
        def raced(*args):
            value = validate(*args)
            epochs.append(self.lease.takeover(self.snapshot.run_id, self.epoch,
                owner={"session_id": "next-owner", "provider": "codex"}))
            return value
        with patch.object(renewed, "_validate_pending_transition_owner", side_effect=raced):
            with self.assertRaises(PermissionError):
                renewed.tick(self.snapshot.run_id, runner)
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(len(runner.start_calls), before)
        self.assertNotEqual(__import__("vibe_guide.state", fromlist=["load_snapshot"]).load_snapshot(
            self.paths, self.snapshot.run_id).authorization_digest, record.digest)

    def check_send_takeover_boundary(self, boundary):
        runner = self.begin_rework_review()
        original = ProviderActionStore._atomic
        epochs = []
        def raced(path, value):
            result = original(path, value)
            if (path.parent.name == "start-dispatches" and path.suffix == ".json"
                    and value.get("phase") == boundary and not epochs):
                epochs.append(self.lease.takeover(self.snapshot.run_id, self.epoch,
                    owner={"session_id": "next-owner", "provider": "codex"}))
            return result
        with patch.object(ProviderActionStore, "_atomic", side_effect=raced), \
                patch.object(runner, "start", wraps=runner.start) as start:
            self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(start.call_count, 0)
        self.assertEqual(self.store.pending(self.snapshot.run_id), [])

    def test_prepared_write_then_takeover_no_start(self):
        self.check_send_takeover_boundary("prepared")

    def test_main_sending_write_then_takeover_no_start(self):
        self.check_send_takeover_boundary("sending")

    def test_start_exception_releases_supervisor_lock(self):
        runner = self.begin_rework_review()
        with patch.object(runner, "start", side_effect=Interrupted()):
            with self.assertRaises(Interrupted):
                self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "next-owner", "provider": "codex"}), self.epoch + 1)
        self.assertEqual(self.store.pending(self.snapshot.run_id), [])

    def test_r4_metadata_cannot_borrow_new_owner(self):
        runner, handle, action = self.mailbox_start_interruption()
        self.finish_resume(action)
        state = self.monitor.tick(self.snapshot.run_id, runner)
        wait, = [a for a in self.store.pending(state.run_id) if a["operation"] == "wait"]
        self.store.complete(wait["action_id"], {"status": "timeout"})
        self.lease.takeover(state.run_id, self.epoch,
            owner={"session_id": "next-owner", "provider": "codex"})
        path = runner._handle_path(handle.run_id)
        metadata = json.loads(path.read_text())
        metadata["start_owner"] = self.monitor._current_dispatch_owner(state.run_id)
        path.write_text(json.dumps(metadata))
        self.monitor.tick(state.run_id, runner)
        self.assertEqual(self.store.pending(state.run_id), [])

    def test_action_transaction_blocks_takeover_until_start_returns(self):
        runner = self.begin_rework_review()
        original = runner.start
        attempted, finished = threading.Event(), threading.Event()
        epochs, errors = [], []
        def takeover():
            attempted.set()
            try:
                epochs.append(self.lease.takeover(self.snapshot.run_id, self.epoch,
                    owner={"session_id": "next-owner", "provider": "codex"}))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        threads = []
        def start(*args):
            thread = threading.Thread(target=takeover)
            threads.append(thread)
            thread.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(finished.wait(0.05), "takeover crossed active send transaction")
            return original(*args)
        try:
            with patch.object(runner, "start", side_effect=start):
                self.monitor.tick(self.snapshot.run_id, runner)
        finally:
            for thread in threads:
                thread.join(5)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(epochs, [self.epoch + 1])


class AuthoritySourceTests(AuthorityFenceTests):
    def test_new_owner_fresh_turn_can_handoff(self):
        runner = self.begin_rework_review()
        self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "next-owner", "provider": "codex"})
        state = self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(state.nodes["n1"]["status"], "rework")
        writer = retirement_writer_lease(self.paths, "n1", ".worktrees/n1", state.run_id)
        self.assertEqual((writer["record"]["role"], writer["record"]["generation"]), ("developer", 2))

    def test_identity_fields_cannot_redirect_poll_sources(self):
        for field, value in (("run_id", "run-unknown-source"), ("node_id", "other-node"),
                ("role", "reviewer"), ("generation", 77), ("task_id", "other-task"),
                ("handle_id", "bridge-other"), ("host", "other-host")):
            with self.subTest(field=field):
                # Each subcase owns a fresh fixture, not a repaired corrupted handle.
                probe = AuthoritySourceTests("test_metadata_run_redirect_must_not_qualify_as_legacy")
                probe.setUp()
                try:
                    runner, handle = probe.waiting_handle()
                    path = runner._handle_path(handle.run_id)
                    metadata = json.loads(path.read_text())
                    metadata[field] = value
                    metadata.pop("start_owner", None)
                    metadata.pop("intent_digest", None)
                    path.write_text(json.dumps(metadata))
                    probe.lease.takeover(probe.snapshot.run_id, probe.epoch,
                        owner={"session_id": "next-owner", "provider": "codex"})
                    before = {p.name for p in probe.store._directory("requests").glob("*.json")}
                    probe.monitor.tick(probe.snapshot.run_id, runner)
                    self.assertEqual({p.name for p in probe.store._directory("requests").glob("*.json")}, before)
                finally:
                    probe.doCleanups()

    def test_live_scope_reauthorization_takeover_no_apply(self):
        runner = self.begin_rework_review()
        self.monitor.tick(self.snapshot.run_id, runner)
        nodes = copy.deepcopy(list(self.monitor.nodes.values()))
        nodes[0].contract["files"].append("tests/r19-scope.py")
        record = authorize(build_authorization_card(self.monitor.plan, nodes,
            self.fixture.capabilities, active_pair_limit=2), "AUTHORIZE")
        renewed = Monitor(self.paths, self.monitor.plan, nodes)
        original = renewed._record
        epochs = []
        def raced(snapshot, event, data, *args, **kwargs):
            result = original(snapshot, event, data, *args, **kwargs)
            if event == "authorization_reauthorized":
                epochs.append(self.lease.takeover(snapshot.run_id, self.epoch,
                    owner={"session_id": "next-owner", "provider": "codex"}))
            return result
        count = len(runner.start_calls)
        with patch.object(renewed, "_record", side_effect=raced):
            with self.assertRaises(PermissionError):
                renewed.reauthorize(self.snapshot.run_id, record, runner, "executable_contract_changed")
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(len(runner.start_calls), count)
        self.assertNotEqual(load_snapshot(self.paths, self.snapshot.run_id).authorization_digest, record.digest)

    def test_preflight_failure_after_takeover_keeps_writer(self):
        runner = self.begin_rework_review()
        before = []
        # Inject outside the provider transaction: profile validation precedes it.
        original_profile = self.monitor._prepare_worker_profile
        def profile(*args):
            result = original_profile(*args)
            before.append(retirement_writer_lease(self.paths, "n1", ".worktrees/n1", self.snapshot.run_id))
            self.lease.takeover(self.snapshot.run_id, self.epoch,
                owner={"session_id": "next-owner", "provider": "codex"})
            raise ValueError("profile failed after takeover")
        with patch.object(self.monitor, "_prepare_worker_profile", side_effect=profile):
            with self.assertRaises(PermissionError):
                self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(retirement_writer_lease(self.paths, "n1", ".worktrees/n1", self.snapshot.run_id), before[0])

    def waiting_handle(self):
        runner, handle, action = self.mailbox_start_interruption()
        self.finish_resume(action)
        state = self.monitor.tick(self.snapshot.run_id, runner)
        wait, = [a for a in self.store.pending(state.run_id) if a['operation'] == 'wait']
        self.store.complete(wait['action_id'], {'status': 'timeout'})
        return runner, handle

    def test_metadata_run_redirect_must_not_qualify_as_legacy(self):
        runner, handle = self.waiting_handle()
        path = runner._handle_path(handle.run_id)
        metadata = json.loads(path.read_text())
        metadata.pop('start_owner')
        metadata.pop('intent_digest')
        redirected = 'run-reviewer-r18-unknown'
        metadata['run_id'] = redirected
        path.write_text(json.dumps(metadata))
        epoch = self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={'session_id': 'next-owner', 'provider': 'codex'})
        self.assertEqual(epoch, self.epoch + 1)
        self.monitor.tick(self.snapshot.run_id, runner)
        actions = self.store.pending(redirected)
        print('REDIRECT', [(a['operation'], a['run_id'], a['sequence']) for a in actions], flush=True)
        self.assertEqual(actions, [], 'mutable run_id hid real start-intent provenance and issued wait')

    def test_handoff_cannot_borrow_owner_after_tick_authorization(self):
        runner = self.begin_rework_review()
        before = retirement_writer_lease(self.paths, 'n1', '.worktrees/n1', self.snapshot.run_id)
        self.assertEqual(before['record']['role'], 'reviewer')
        original = self.monitor._handoff_delivery_writer
        epochs = []
        def raced(snapshot, node_id, role='reviewer'):
            if role == 'developer' and not epochs:
                self.assertEqual(snapshot._dispatch_owner['lease_epoch'], self.epoch)
                epochs.append(self.lease.takeover(snapshot.run_id, self.epoch,
                    owner={'session_id': 'next-owner', 'provider': 'codex'}))
            return original(snapshot, node_id, role)
        count = len(runner.start_calls)
        with patch.object(self.monitor, '_handoff_delivery_writer', side_effect=raced):
            self.monitor.tick(self.snapshot.run_id, runner)
        after = retirement_writer_lease(self.paths, 'n1', '.worktrees/n1', self.snapshot.run_id)
        files = [json.loads(p.read_text()) for p in
            (self.paths.vibe / 'runs' / self.snapshot.run_id / 'writer-handoffs').glob('*.json')]
        print('HANDOFF', 'takeover', epochs, 'before', (before['record']['role'], before['record']['generation']),
            'after', None if after is None else (after['record']['role'], after['record']['generation']),
            'starts', count, len(runner.start_calls), 'new-owner-intents',
            [(v['phase'], v['intent']['owner']['lease_epoch']) for v in files if v['intent']['owner']['lease_epoch'] != self.epoch],
            'reason', load_snapshot(self.paths, self.snapshot.run_id).nodes['n1'].get('reason'), flush=True)
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(len(runner.start_calls), count)
        self.assertEqual([v for v in files if v['intent']['owner']['lease_epoch'] != self.epoch], [],
            'old tick committed handoff with new owner sampled after takeover')
        self.assertEqual(after, before, 'old tick committed writer handoff under resampled new owner')

    def test_live_reauthorization_event_takeover_no_apply(self):
        runner = self.begin_rework_review()
        self.monitor.tick(self.snapshot.run_id, runner)
        nodes = copy.deepcopy(list(self.monitor.nodes.values()))
        record = authorize(build_authorization_card(self.monitor.plan, nodes,
            self.fixture.capabilities, active_pair_limit=2), 'AUTHORIZE')
        renewed = Monitor(self.paths, self.monitor.plan, nodes)
        original = renewed._record
        epochs, proof_epochs = [], []
        def raced(snapshot, event, data, *args, **kwargs):
            value = original(snapshot, event, data, *args, **kwargs)
            if event == 'authorization_reauthorized':
                proof_epochs.append(data['proof']['owner']['lease_epoch'])
                epochs.append(self.lease.takeover(snapshot.run_id, self.epoch,
                    owner={'session_id': 'next-owner', 'provider': 'codex'}))
            return value
        count = len(runner.start_calls)
        with patch.object(renewed, '_record', side_effect=raced):
            try:
                renewed.reauthorize(self.snapshot.run_id, record, runner, 'executable_contract_changed')
            except PermissionError:
                pass
        state = load_snapshot(self.paths, self.snapshot.run_id)
        print('LIVE_REAUTH', 'proof', proof_epochs, 'takeover', epochs, 'starts',
            count, len(runner.start_calls), 'new_authority', state.authorization_digest == record.digest, flush=True)
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(len(runner.start_calls), count, 'old live reauthorize dispatched after takeover')
        self.assertNotEqual(state.authorization_digest, record.digest, 'old live reauthorize committed stale proof authority')

    def test_missing_dispatch_does_not_issue_wait(self):
        runner, handle = self.waiting_handle()
        directory = self.paths.vibe / 'runs' / self.snapshot.run_id / 'start-dispatches'
        for path in directory.glob('*.json'):
            path.unlink()
        self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(self.store.pending(self.snapshot.run_id), [])

    def test_after_start_takeover_never_issues_wait_or_second_start(self):
        runner = self.begin_rework_review()
        original = self.monitor._dispatch_with_intent
        from contextlib import contextmanager
        from vibe_guide.supervisor import SupervisorLease
        lock = SupervisorLease.lock
        sent, epochs, triggered = [], [], []
        def started(*args):
            value = original(*args)
            sent.append(True)
            return value
        @contextmanager
        def wrapped(lease, run_id):
            with lock(lease, run_id):
                yield
            if sent and not triggered:
                triggered.append(True)
                epochs.append(self.lease.takeover(run_id, self.epoch,
                    owner={'session_id': 'next-owner', 'provider': 'codex'}))
        count = len(runner.start_calls)
        with patch.object(self.monitor, '_dispatch_with_intent', side_effect=started), \
             patch.object(SupervisorLease, 'lock', wrapped):
            self.monitor.tick(self.snapshot.run_id, runner)
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(len(runner.start_calls), count + 1)
        self.assertEqual(self.store.pending(self.snapshot.run_id), [])


class ProtectedSuccessorTests(unittest.TestCase):
    def setUp(self):
        self.probe = NativeRecoveryTerminalTests("test_retired_developer_creates_one_visible_successor_through_normal_scheduler")
        p = self.probe
        def prepare(current):
            import subprocess
            root = p.paths.root / current.contract["worktree"]
            root.mkdir(parents=True)
            branch = current.contract["worker_profile"]["branch"]
            for args in (("init", "-b", branch), ("commit", "--allow-empty", "-m", "baseline")):
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.test", *args], check=True, capture_output=True)
            (root / "tracked.py").write_text("baseline = True\n")
            for args in (("add", "tracked.py"), ("commit", "-m", "tracked baseline")):
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.test", *args], check=True, capture_output=True)
            head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            current.contract.update(binding_contract_version="3.9", dispatch_topology="visible-sdd",
                managed_root=str(p.paths.root), worktree=str(root), base_sha=head, branch=branch)
            current.contract["worker_profile"]["worktree"] = str(root)
        def binding(contract, worktree, run_id, status):
            from vibe_guide.models import BindingIntent, BindingObservation, WaitThreadsCursorObservation
            from vibe_guide.state import read_writer_lease
            from vibe_guide.task_registry import TaskBinding, runtime_binding_gate
            lease = read_writer_lease(p.paths, "n1", str(worktree))
            cursor = WaitThreadsCursorObservation.from_wait_threads("original-worker", "local", "original-cursor")
            identity = dict(project_id="fixture-project", task_id="original-worker", host_id="local",
                node_id="n1", worktree=str(worktree), managed_root=str(p.paths.root),
                branch=contract["branch"], base_sha=contract["base_sha"],
                head_sha=contract["base_sha"], clean=True, cursor=cursor.cursor)
            intent = BindingIntent(**identity, lease_id=lease.lease_id)
            observation = BindingObservation(**identity, lease=lease, source="codex_app__wait_threads",
                cursor_source="codex_app__wait_threads", cursor_task_id="original-worker",
                cursor_host_id="local", cursor_lineage=cursor.lineage, cursor_observation=cursor)
            contract.update(binding_intent=intent, binding_observation=observation)
            result = TaskBinding(provider="codex-app-visible", mode="visible", topology="visible-sdd",
                task_id="original-worker", host="local", issue_id="n1", role="developer",
                worktree=str(worktree), branch=contract["branch"], run_id=run_id, status=status,
                generation=contract["generation"], allowlist=contract["files"], cursor=cursor.cursor,
                binding_intent=intent, binding_observation=observation)
            self.assertTrue(runtime_binding_gate(contract, result).verified)
            return result
        p.prepare_protected_node = prepare
        p.protected_binding = binding
        p.setUp()
        self.addCleanup(self.probe.doCleanups)

    def test_protected_visible_sdd_retirement_enters_normal_live_binding(self):
        p = self.probe
        action, observation, receipt, preserved, retire = p.retirement_fixture()
        retire()
        monitor = p.monitor
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        verified = []
        original_gate = runner.binding_gate
        def gate(contract, binding):
            result = original_gate(contract, binding)
            if result.verified:
                verified.append((binding.binding_state, binding.business_write_allowed,
                    binding.binding_observation.clean, binding.binding_observation.cursor))
            return result
        runner.binding_gate = gate
        state = monitor.tick(p.snapshot.run_id, runner)
        for operation, payload in (("create", {"threadId": "protected-successor", "hostId": "local"}),
                ("wait", {"polls": [{"thread": {"id": "protected-successor", "hostId": "local"},
                                      "cursor": "protected-cursor"}]}),
                ("locate", {"located": True}),
                ("visibility", {"visible": True, "direct_enter": True})):
            pending = [item for item in p.store.pending(state.run_id) if item["operation"] == operation]
            self.assertEqual(len(pending), 1, state.nodes["n1"].get("reason"))
            state = monitor.tick(state.run_id, runner)
            replay = [item for item in p.store.pending(state.run_id) if item["operation"] == operation]
            self.assertEqual([item["action_id"] for item in replay], [pending[0]["action_id"]])
            complete_native_mailbox_action(p.paths, state.run_id, pending[0]["action_id"],
                {"structuredContent": payload, "isError": False}, request_digest=pending[0]["request_digest"])
            state = monitor.tick(state.run_id, runner)
        bound = load_task_binding(p.paths, "n1", "developer", run_id=state.run_id)
        self.assertEqual(bound.task_id, "protected-successor")
        self.assertEqual(bound.successor_of, p.binding.task_id)
        self.assertEqual(bound.topology, "visible-sdd")
        self.assertEqual(bound.generation, p.binding.generation + 1)
        self.assertTrue(verified)
        self.assertIn(("binding_verified", True, False, "protected-cursor"), verified)
        self.assertEqual(state.nodes["n1"]["active_task"]["task_id"], "protected-successor")
        self.assertIn("n1", state.handles)
        self.assertFalse(bound.business_write_allowed)  # JSON reload never restores live authority.
        self.assertEqual(monitor.successor_preservation(state.run_id, "n1"), preserved)
        creates = [p.store._read(path) for path in p.store._directory("requests").glob("*.json")]
        self.assertEqual(len([row for row in creates if row["operation"] == "create"]), 1)

    def successor_probe(self):
        p = self.probe
        action, observation, receipt, preserved, retire = p.retirement_fixture()
        retire()
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        state = p.monitor.tick(p.snapshot.run_id, runner)
        create, = [item for item in p.store.pending(state.run_id) if item["operation"] == "create"]
        complete_native_mailbox_action(p.paths, state.run_id, create["action_id"],
            {"structuredContent": {"threadId": "protected-successor", "hostId": "local"}},
            request_digest=create["request_digest"])
        state = p.monitor.tick(state.run_id, runner)
        wait, = [item for item in p.store.pending(state.run_id) if item["operation"] == "wait"]
        return runner, state, create, wait

    def assert_no_locate(self, runner, state):
        p = self.probe
        state = p.monitor.tick(state.run_id, runner)
        pending = p.store.pending(state.run_id)
        self.assertFalse(any(item["operation"] in {"locate", "visibility"} for item in pending))
        self.assertEqual(load_task_binding(p.paths, "n1", "developer",
            run_id=state.run_id).task_id, p.binding.task_id)
        return state

    def test_wait_cursor_wrong_task_blocks_before_locate(self):
        runner, state, create, wait = self.successor_probe()
        complete_native_mailbox_action(self.probe.paths, state.run_id, wait["action_id"],
            {"structuredContent": {"polls": [{"thread": {"id": "original-worker", "hostId": "local"},
                "cursor": "forged"}]}}, request_digest=wait["request_digest"])
        self.assert_no_locate(runner, state)

    def test_wait_projection_without_full_raw_blocks_before_locate(self):
        runner, state, create, wait = self.successor_probe()
        self.probe.store.complete(wait["action_id"], {"polls": [{"thread": {"id": "protected-successor",
            "hostId": "local"}, "cursor": "forged"}]}, request_digest=wait["request_digest"])
        self.assert_no_locate(runner, state)

    def test_preserved_content_drift_blocks_before_locate(self):
        runner, state, create, wait = self.successor_probe()
        (Path(self.probe.binding.worktree) / "n1.py").write_text("unexpected change")
        self.assert_no_locate(runner, state)

    def test_old_supervisor_blocks_before_locate(self):
        runner, state, create, wait = self.successor_probe()
        self.probe.lease.takeover(state.run_id, self.probe.epoch,
            owner={"session_id": "new-supervisor", "provider": "codex"})
        self.assert_no_locate(runner, state)

    def test_writer_lease_generation_drift_blocks_before_locate(self):
        runner, state, create, wait = self.successor_probe()
        from vibe_guide.state import retirement_writer_lease
        p = self.probe
        held = retirement_writer_lease(p.paths, "n1", p.binding.worktree, state.run_id)
        path = p.paths.vibe / "leases" / held["filename"]
        row = json.loads(path.read_text())
        row["generation"] -= 1
        path.write_text(json.dumps(row))
        self.assert_no_locate(runner, state)

    def test_successor_root_drift_blocks_before_create(self):
        p = self.probe
        action, observation, receipt, preserved, retire = p.retirement_fixture()
        retire()
        state = load_snapshot(p.paths, p.snapshot.run_id)
        state.nodes["n1"]["worktree"] = str(p.paths.root / "different-root")
        from vibe_guide.state import save_snapshot
        save_snapshot(p.paths, state)
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        with patch.object(runner, "_action") as effect:
            p.monitor.tick(state.run_id, runner)
            effect.assert_not_called()

    def test_successor_branch_drift_blocks_before_create(self):
        import subprocess
        p = self.probe
        action, observation, receipt, preserved, retire = p.retirement_fixture()
        retire()
        subprocess.run(["git", "-C", p.binding.worktree, "branch", "-m", "unexpected-branch"],
                       check=True, capture_output=True)
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        with patch.object(runner, "_action") as effect:
            p.monitor.tick(p.snapshot.run_id, runner)
            effect.assert_not_called()

    def test_cursor_drift_after_live_probe_blocks_before_visibility(self):
        p = self.probe
        runner, state, create, wait = self.successor_probe()
        complete_native_mailbox_action(p.paths, state.run_id, wait["action_id"],
            {"structuredContent": {"polls": [{"thread": {"id": "protected-successor", "hostId": "local"},
                "cursor": "protected-cursor"}]}}, request_digest=wait["request_digest"])
        original = runner._require_result
        def drift(contract, run_id, operation, request):
            if operation == "locate":
                from vibe_guide.task_registry import runtime_binding_gate
                raw_path = p.store._native_receipt_path(wait["action_id"])
                raw = p.store._read(raw_path)
                raw["native_response"]["structuredContent"]["polls"][0]["cursor"] = "drifted"
                from vibe_guide.adapters.task_provider import _canonical_digest
                raw["receipt_digest"] = _canonical_digest({k: v for k, v in raw.items() if k != "receipt_digest"})
                p.store._atomic(raw_path, raw)
                self.assertFalse(runtime_binding_gate(contract).verified)
                raise __import__("vibe_guide.adapters.task_provider", fromlist=["ProviderUnavailable"]).ProviderUnavailable("cursor drift")
            return original(contract, run_id, operation, request)
        runner._require_result = drift
        self.assert_no_locate(runner, state)

    def test_visible_sdd_reviewer_rejected_before_provider(self):
        p = self.probe
        action, observation, receipt, preserved, retire = p.retirement_fixture()
        retire()
        state = load_snapshot(p.paths, p.snapshot.run_id)
        state._dispatch_owner = p.monitor._current_dispatch_owner(state.run_id)
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        with patch.object(runner, "_action") as effect:
            self.assertFalse(p.monitor._start_task(state, "n1", "reviewer", "review",
                runner, continuation=False, successor=True))
            effect.assert_not_called()

    def test_unprotected_dirty_evidence_keeps_clean_only_gate(self):
        from dataclasses import replace
        from vibe_guide.task_registry import validate_binding
        p = self.probe
        # The original public gate established this clean binding in setUp.
        binding = p.initial_runner.start_calls[0]
        intent = binding["binding_intent"]
        observation = binding["binding_observation"]
        self.assertFalse(validate_binding(replace(intent, clean=False),
            replace(observation, clean=False)).verified)


class FreshOwnerRetirementTests(unittest.TestCase):
    def setUp(self):
        self.case = ProtectedSuccessorTests("test_protected_visible_sdd_retirement_enters_normal_live_binding")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.p = self.case.probe

    def prepared(self):
        p = self.p
        action, observation, receipt, preserved, retire = p.retirement_fixture()
        path = p.paths.vibe / "runs" / p.snapshot.run_id / "successor-retirements" / (action["action_id"] + ".json")
        atomic = ProviderActionStore._atomic
        def crash(target, payload):
            atomic(target, payload)
            if target == path:
                raise RuntimeError("prepared crash")
        with patch.object(ProviderActionStore, "_atomic", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "prepared crash"):
                retire()
        return action, observation, receipt, preserved, path, json.loads(path.read_text())

    def fresh(self, observation, receipt, session="supervisor-2", previous_epoch=None):
        from vibe_guide.supervisor import _record_digest
        p = self.p
        epoch = p.lease.takeover(p.snapshot.run_id, p.epoch if previous_epoch is None else previous_epoch,
            owner={"session_id": session, "provider": "codex"})
        observer = p.observer.with_name("rollout-test-" + session + ".jsonl")
        rows = copy.deepcopy(p.observer_rows)
        rows[0]["payload"]["id"] = session
        observer.write_text("".join(json.dumps(row) + "\n" for row in rows))
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        source = json.loads(receipt.read_text())
        source["observer"] = {"session_id": session, "lease_epoch": epoch,
            "session_record": str(observer), "original_session_record": str(observer),
            "source_prefix_bytes": observer.stat().st_size, "source_prefix_sha256": sha(observer)}
        fresh_receipt = receipt.with_name(session + "-termination.json")
        fresh_receipt.write_text(json.dumps(source))
        registry_path = p.paths.vibe / "runs" / p.snapshot.run_id / "supervisor-registry.json"
        registry = p.store._read(registry_path)
        registry["history"].append(registry["current"])
        registry["current"] = {"provider": "codex", "session_id": session, "host": "local",
            "lease_epoch": epoch, "session_record_digest": _record_digest(observer)}
        p.store._atomic(registry_path, registry)
        fresh = copy.deepcopy(observation)
        fresh["native_terminal"].update(receipt_path=str(fresh_receipt), receipt_sha256=sha(fresh_receipt),
            source_rollout_path=str(observer), source_rollout_sha256=sha(observer))
        return epoch, fresh

    def finish_successor(self, preserved):
        p = self.p
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        state = p.monitor.tick(p.snapshot.run_id, runner)
        for operation, payload in (("create", {"threadId": "protected-successor", "hostId": "local"}),
                ("wait", {"polls": [{"thread": {"id": "protected-successor", "hostId": "local"},
                                      "cursor": "protected-cursor"}]}),
                ("locate", {"located": True}), ("visibility", {"visible": True, "direct_enter": True})):
            pending = [row for row in p.store.pending(state.run_id) if row["operation"] == operation]
            self.assertEqual(len(pending), 1, state.nodes["n1"].get("reason"))
            state = p.monitor.tick(state.run_id, runner)
            self.assertEqual([row["action_id"] for row in p.store.pending(state.run_id) if row["operation"] == operation],
                [pending[0]["action_id"]])
            complete_native_mailbox_action(p.paths, state.run_id, pending[0]["action_id"],
                {"structuredContent": payload}, request_digest=pending[0]["request_digest"])
            state = p.monitor.tick(state.run_id, runner)
        bound = load_task_binding(p.paths, "n1", "developer", run_id=state.run_id)
        self.assertEqual(bound.task_id, "protected-successor")
        self.assertEqual(bound.generation, p.binding.generation + 1)
        self.assertEqual(bound.successor_of, p.binding.task_id)
        self.assertEqual(state.nodes["n1"]["active_task"]["task_id"], bound.task_id)
        self.assertIn("n1", state.handles)
        self.assertEqual(p.monitor.successor_preservation(state.run_id, "n1"), preserved)
        requests = [p.store._read(path) for path in p.store._directory("requests").glob("*.json")]
        self.assertEqual(len([row for row in requests if row["operation"] == "create"]), 1)

    def test_prepared_fresh_owner_completes_same_transaction_and_protected_successor(self):
        from vibe_guide.state import load_events
        p = self.p
        action, observation, receipt, preserved, path, before = self.prepared()
        original_receipt = receipt.read_bytes()
        request = p.store.action(action["action_id"])
        epoch, fresh = self.fresh(observation, receipt)
        self.assertEqual(p.store.verify_successor_termination(action["action_id"], fresh,
            owner_epoch=epoch, owner_session_id="supervisor-2")["status"], "verified")
        result = p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
            owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        self.assertEqual(result["status"], "retired")
        self.assertEqual(result, p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
            owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved))
        self.assertEqual(p.store.action(action["action_id"]), request)
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self.assertEqual(json.loads(path.read_text())["intent"], before["intent"])
        self.assertEqual(len([row for row in load_events(p.paths, p.snapshot.run_id)
            if row["event"] == "verified_successor_retired"]), 1)
        self.finish_successor(preserved)

    def migration_crash(self, boundary):
        from vibe_guide import monitor as module
        from vibe_guide.state import load_events
        p = self.p
        action, observation, receipt, preserved, path, before = self.prepared()
        epoch, fresh = self.fresh(observation, receipt)
        def retire():
            return p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        if boundary in {"migration-before", "migration-after", "commit"}:
            target, name = ProviderActionStore, "_atomic"
            original = target._atomic
            def injected(dest, payload):
                trigger = dest == path and bool(payload.get("owner_observations")) and (
                    payload.get("phase") == ("committed" if boundary == "commit" else "prepared"))
                if trigger and boundary == "migration-before":
                    raise RuntimeError("migration crash")
                original(dest, payload)
                if trigger:
                    raise RuntimeError("migration crash")
        elif boundary in {"binding", "lease"}:
            target, name = module, ("save_task_binding" if boundary == "binding" else "release_writer_lease")
            original = getattr(target, name)
            def injected(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("migration crash")
        elif boundary == "event":
            target, name = p.monitor, "_record"
            original = target._record
            def injected(snapshot, event, data, *args, **kwargs):
                original(snapshot, event, data, *args, **kwargs)
                if event == "verified_successor_retired":
                    raise RuntimeError("migration crash")
        else:
            target, name = p.monitor, "_save_snapshot"
            original = target._save_snapshot
            def injected(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("migration crash")
        with patch.object(target, name, side_effect=injected):
            with self.assertRaisesRegex(RuntimeError, "migration crash"):
                retire()
        result = retire()
        self.assertEqual(result, retire())
        saved = json.loads(path.read_text())
        self.assertEqual(saved["phase"], "committed")
        self.assertEqual(saved["intent"], before["intent"])
        self.assertEqual(len(saved["owner_observations"]), 1)
        self.assertIsNone(retirement_writer_lease(p.paths, "n1", p.binding.worktree, p.snapshot.run_id))
        self.assertEqual(len([row for row in load_events(p.paths, p.snapshot.run_id)
            if row["event"] == "verified_successor_retired"]), 1)
        self.assertEqual(p.monitor.successor_preservation(p.snapshot.run_id, "n1"), preserved)
        self.finish_successor(preserved)

    def test_migration_prepare_crash_before_persistence(self):
        self.migration_crash("migration-before")

    def test_migration_prepare_crash_after_persistence(self):
        self.migration_crash("migration-after")

    def test_migration_replays_after_binding_stop(self):
        self.migration_crash("binding")

    def test_migration_replays_after_lease_release(self):
        self.migration_crash("lease")

    def test_migration_replays_after_event(self):
        self.migration_crash("event")

    def test_migration_replays_after_snapshot(self):
        self.migration_crash("snapshot")

    def test_migration_replays_after_commit(self):
        self.migration_crash("commit")

    def test_invalid_fresh_observations_and_foreign_lease_have_zero_retirement_effects(self):
        p = self.p
        action, observation, receipt, preserved, path, before = self.prepared()
        epoch, fresh = self.fresh(observation, receipt)
        bound = load_task_binding(p.paths, "n1", "developer", run_id=p.snapshot.run_id).to_dict()
        lease_path = p.paths.vibe / "leases" / before["intent"]["writer_lease"]["filename"]
        lease_bytes = lease_path.read_bytes()
        old = path.read_bytes()
        for field, value in (("session_id", "foreign-task"), ("host", "foreign-host"), ("native_terminal", {})):
            bad = copy.deepcopy(fresh)
            bad[field] = value
            with self.subTest(field=field), self.assertRaises((PermissionError, ValueError, KeyError)):
                p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], bad,
                    owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        with self.assertRaises((PermissionError, ValueError)):
            p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], observation,
                owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        foreign = json.loads(lease_bytes)
        foreign["run_id"] = "foreign-run"
        lease_path.write_text(json.dumps(foreign))
        foreign_bytes = lease_path.read_bytes()
        with self.assertRaises((PermissionError, ValueError)):
            p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        self.assertEqual(lease_path.read_bytes(), foreign_bytes)
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(load_task_binding(p.paths, "n1", "developer", run_id=p.snapshot.run_id).to_dict(), bound)
        self.assertEqual(p.monitor.successor_preservation(p.snapshot.run_id, "n1"), preserved)

    def test_owner_changes_after_migration_persist_old_owner_cannot_retire(self):
        p = self.p
        action, observation, receipt, preserved, path, before = self.prepared()
        epoch, fresh = self.fresh(observation, receipt)
        atomic = ProviderActionStore._atomic
        def raced(target, payload):
            atomic(target, payload)
            if target == path and payload.get("owner_observations"):
                raise RuntimeError("migration persisted before takeover")
        with patch.object(ProviderActionStore, "_atomic", side_effect=raced):
            with self.assertRaisesRegex(RuntimeError, "migration persisted"):
                p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                    owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        self.assertEqual(load_task_binding(p.paths, "n1", "developer", run_id=p.snapshot.run_id).status, p.binding.status)
        self.assertEqual(json.loads(path.read_text())["phase"], "prepared")
        third_epoch, third = self.fresh(observation, receipt, session="supervisor-3", previous_epoch=epoch)
        with self.assertRaisesRegex(PermissionError, "owner"):
            p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        result = p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], third,
            owner_epoch=third_epoch, owner_session_id="supervisor-3", frozen_preservation=preserved)
        self.assertEqual(result["status"], "retired")
        saved = json.loads(path.read_text())
        self.assertEqual(saved["intent"], before["intent"])
        self.assertEqual(len(saved["owner_observations"]), 2)
        self.finish_successor(preserved)

    def test_fresh_owner_lineage_source_and_contract_conflicts_do_not_mutate_transaction(self):
        p = self.p
        action, observation, receipt, preserved, path, before = self.prepared()
        epoch, fresh = self.fresh(observation, receipt)
        task_path = p.paths.vibe / "runs" / p.snapshot.run_id / "tasks.json"
        original = task_path.read_bytes()
        intent_bytes = path.read_bytes()
        def retire():
            return p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        for field, value in (("generation", 999), ("provider", "foreign-provider"),
                             ("threadId", "foreign-task"), ("hostId", "foreign-host"),
                             ("role", "reviewer")):
            record = json.loads(original)
            for row in record["bindings"]:
                row[field] = value
            task_path.write_text(json.dumps(record))
            with self.subTest(field=field), self.assertRaises((PermissionError, ValueError, FileNotFoundError)):
                retire()
            self.assertEqual(path.read_bytes(), intent_bytes)
            task_path.write_bytes(original)
        source = Path(fresh["native_terminal"]["receipt_path"] )
        source_bytes = source.read_bytes()
        source.write_bytes(source_bytes + b" " )
        with self.assertRaises((PermissionError, ValueError)):
            retire()
        source.write_bytes(source_bytes)
        contract = p.monitor.nodes["n1"].contract
        old_files = contract["files"]
        contract["files"] = ["foreign.py"]
        with self.assertRaises((PermissionError, ValueError)):
            retire()
        contract["files"] = old_files
        self.assertEqual(path.read_bytes(), intent_bytes)
        self.assertEqual(task_path.read_bytes(), original)
        self.assertEqual(p.monitor.successor_preservation(p.snapshot.run_id, "n1"), preserved)

    def test_public_cli_fresh_owner_migration_and_repeat_are_idempotent(self):
        p = self.p
        action, observation, receipt, preserved, path, before = self.prepared()
        epoch, fresh = self.fresh(observation, receipt)
        payload = p.paths.root / "fresh-retire-input.json"
        payload.write_text(json.dumps({"observation": fresh, "frozen_preservation": preserved}))
        args = ["supervisor-mailbox", "--project-root", str(p.paths.root), "--run-id", p.snapshot.run_id,
            "--retire-successor", action["action_id"], "--payload", str(payload), "--json"]
        with patch("vibe_guide.cli._load_plan", return_value=(None, p.monitor.plan, list(p.monitor.nodes.values()), None)), \
                patch("vibe_guide.cli._observed_topology_rulings", return_value={}), \
                patch("vibe_guide.supervisor.caller_session_ids", return_value=("supervisor-2",)):
            for _ in range(2):
                result = run_cli(args, p.paths.root)
                self.assertEqual(result.exit_code, 0, result.payload)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["intent"], before["intent"])
        self.assertEqual(len(saved["owner_observations"]), 1)
        self.finish_successor(preserved)


class FreshOwnerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.case = FreshOwnerRetirementTests("test_prepared_fresh_owner_completes_same_transaction_and_protected_successor")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.p = self.case.p

    def empty_committed_history(self):
        p = self.p
        action, observation, receipt, preserved, retire = p.retirement_fixture()
        retire()
        path = (p.paths.vibe / "runs" / p.snapshot.run_id / "successor-retirements"
                / (action["action_id"] + ".json"))
        saved = json.loads(path.read_text())
        saved["owner_observations"] = []
        path.write_text(json.dumps(saved))
        return receipt, path, path.read_bytes()

    def test_empty_history_changed_source_keeps_dispatch_blocked_unknown(self):
        p = self.p
        receipt, path, frozen = self.empty_committed_history()
        receipt.write_bytes(receipt.read_bytes() + b" ")
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        with patch.object(runner, "_action") as effect:
            state = p.monitor.tick(p.snapshot.run_id, runner)
            effect.assert_not_called()
        self.assertEqual(state.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(path.read_bytes(), frozen)

    def test_empty_history_root_drift_keeps_zero_create_boundary(self):
        from vibe_guide.state import save_snapshot
        p = self.p
        receipt, path, frozen = self.empty_committed_history()
        state = load_snapshot(p.paths, p.snapshot.run_id)
        state.nodes["n1"]["worktree"] = str(p.paths.root / "different-root")
        save_snapshot(p.paths, state)
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        with patch.object(runner, "_action") as effect:
            state = p.monitor.tick(state.run_id, runner)
            effect.assert_not_called()
        self.assertEqual(path.read_bytes(), frozen)

    def migrated_prepared(self):
        p = self.p
        action, observation, receipt, preserved, path, before = self.case.prepared()
        epoch, fresh = self.case.fresh(observation, receipt)
        atomic = ProviderActionStore._atomic
        def crash(target, payload):
            atomic(target, payload)
            if target == path and payload.get("owner_observations"):
                raise RuntimeError("migration prepared")
        with patch.object(ProviderActionStore, "_atomic", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "migration prepared"):
                p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                    owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        return action, observation, receipt, preserved, path, before, epoch, fresh

    def corrupt_history(self, wrong_owner=False):
        from vibe_guide.state import load_events
        p = self.p
        action, observation, receipt, preserved, path, before, epoch, fresh = self.migrated_prepared()
        saved = json.loads(path.read_text())
        row = saved["owner_observations"][0]
        if wrong_owner:
            row["owner_session_id"] = "foreign-owner-not-observer"
        else:
            row["proof"]["termination"]["receipt_sha256"] = "f" * 64
        row["digest"] = digests.canonical_json_digest({k: v for k, v in row.items() if k != "digest"})
        path.write_text(json.dumps(saved))
        session = "supervisor-2"
        if not wrong_owner:
            epoch, fresh = self.case.fresh(observation, receipt, session="supervisor-3", previous_epoch=epoch)
            session = "supervisor-3"
        prior = path.read_bytes()
        registry = p.paths.vibe / "runs" / p.snapshot.run_id / "tasks.json"
        registry_bytes = registry.read_bytes()
        with self.assertRaises((PermissionError, ValueError)):
            p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                owner_epoch=epoch, owner_session_id=session, frozen_preservation=preserved)
        self.assertEqual(path.read_bytes(), prior)
        self.assertEqual(registry.read_bytes(), registry_bytes)
        self.assertFalse(any(row["event"] == "verified_successor_retired" for row in load_events(p.paths, p.snapshot.run_id)))

    def test_history_owner_requires_independent_observer_binding(self):
        self.corrupt_history(wrong_owner=True)

    def test_third_owner_cannot_cover_replaced_historical_proof(self):
        self.corrupt_history()

    def test_original_intent_proof_and_registry_history_are_independently_required(self):
        p = self.p
        action, observation, receipt, preserved, path, before, epoch, fresh = self.migrated_prepared()
        original = path.read_bytes()
        registry = p.paths.vibe / "runs" / p.snapshot.run_id / "supervisor-registry.json"
        registry_bytes = registry.read_bytes()
        for change in ("original-proof", "original-source", "history-registry"):
            with self.subTest(change=change):
                if change == "original-proof":
                    saved = json.loads(original)
                    saved["intent"]["proof"]["termination"]["receipt_sha256"] = "f" * 64
                    saved["intent"]["digest"] = digests.canonical_json_digest(
                        {k: v for k, v in saved["intent"].items() if k != "digest"})
                    row = saved["owner_observations"][0]
                    row["previous_digest"] = saved["intent"]["digest"]
                    row["digest"] = digests.canonical_json_digest({k: v for k, v in row.items() if k != "digest"})
                    path.write_text(json.dumps(saved))
                elif change == "original-source":
                    receipt_bytes = receipt.read_bytes()
                    receipt.write_bytes(receipt_bytes + b" " )
                else:
                    record = json.loads(registry_bytes)
                    record["history"] = []
                    registry.write_text(json.dumps(record))
                frozen = path.read_bytes()
                bound = load_task_binding(p.paths, "n1", "developer", run_id=p.snapshot.run_id).to_dict()
                with self.assertRaises((PermissionError, ValueError)):
                    p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                        owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
                self.assertEqual(path.read_bytes(), frozen)
                self.assertEqual(load_task_binding(p.paths, "n1", "developer", run_id=p.snapshot.run_id).to_dict(), bound)
                path.write_bytes(original)
                registry.write_bytes(registry_bytes)
                if change == "original-source":
                    receipt.write_bytes(receipt_bytes)

    def test_old_snapshot_replay_authenticates_history_and_keeps_unique_successor(self):
        from vibe_guide.state import load_events
        p = self.p
        action, observation, receipt, preserved, path, before, epoch, fresh = self.migrated_prepared()
        state_path = p.paths.vibe / "runs" / p.snapshot.run_id / "state.json"
        old = state_path.read_bytes()
        def retire():
            return p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
                owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        result = retire()
        transaction = path.read_bytes()
        state_path.write_bytes(old)
        self.assertEqual(retire(), result)
        self.assertEqual(path.read_bytes(), transaction)
        self.assertEqual(sum(row["event"] == "verified_successor_retired" for row in load_events(p.paths, p.snapshot.run_id)), 1)
        self.case.finish_successor(preserved)

    def test_changed_history_source_after_scope_creation_blocks_final_binding(self):
        p = self.p
        action, observation, receipt, preserved, path, before, epoch, fresh = self.migrated_prepared()
        p.monitor.retire_verified_successor(p.snapshot.run_id, action["action_id"], fresh,
            owner_epoch=epoch, owner_session_id="supervisor-2", frozen_preservation=preserved)
        runner = ProviderActionRunner(p.paths, p.binding.provider, "codex-app-visible")
        state = p.monitor.tick(p.snapshot.run_id, runner)
        for operation, payload in (("create", {"threadId": "protected-successor", "hostId": "local"}),
                ("wait", {"polls": [{"thread": {"id": "protected-successor", "hostId": "local"},
                    "cursor": "protected-cursor"}]}), ("locate", {"located": True})):
            pending, = [row for row in p.store.pending(state.run_id) if row["operation"] == operation]
            complete_native_mailbox_action(p.paths, state.run_id, pending["action_id"],
                {"structuredContent": payload}, request_digest=pending["request_digest"])
            state = p.monitor.tick(state.run_id, runner)
        visible, = [row for row in p.store.pending(state.run_id) if row["operation"] == "visibility"]
        complete_native_mailbox_action(p.paths, state.run_id, visible["action_id"],
            {"structuredContent": {"visible": True, "direct_enter": True}}, request_digest=visible["request_digest"])
        original = p.monitor._save_task_binding
        seen = []
        def consume(binding):
            if binding.task_id == "protected-successor":
                seen.append(True)
                receipt.write_bytes(receipt.read_bytes() + b" " )
            return original(binding)
        with patch.object(p.monitor, "_save_task_binding", consume):
            state = p.monitor.tick(state.run_id, runner)
        self.assertEqual(seen, [True])
        self.assertEqual(load_task_binding(p.paths, "n1", "developer", run_id=state.run_id).task_id, p.binding.task_id)
        self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])
        self.assertIsNone(state.nodes["n1"].get("active_task"))


class ProtectedFenceTests(unittest.TestCase):
    def setUp(self):
        self.case = ProtectedSuccessorTests("test_protected_visible_sdd_retirement_enters_normal_live_binding")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.p = self.case.probe

    def ready_locate(self):
        runner, state, create, wait = self.case.successor_probe()
        complete_native_mailbox_action(self.p.paths, state.run_id, wait["action_id"],
            {"structuredContent": {"polls": [{"thread": {"id": "protected-successor", "hostId": "local"},
                "cursor": "protected-cursor"}]}}, request_digest=wait["request_digest"])
        return runner, state

    def test_active_scope_rejects_wrong_contract_runner_and_thread(self):
        import contextvars
        from vibe_guide.task_registry import runtime_binding_gate, TaskBinding
        runner, state = self.ready_locate()
        original = runner._require_result
        checked = []
        def observe(contract, run_id, operation, request):
            if operation == "locate":
                self.assertTrue(runtime_binding_gate(contract).verified)
                for overrides in ({"node_id": "other"}, {"role": "reviewer"}, {"generation": 999},
                                  {"files": ["foreign.py"]}, {"run_id": "other-run"},
                                  {"predecessor_task_id": "other"}, {"authorization_digest": "other"}):
                    self.assertFalse(runtime_binding_gate(dict(contract, **overrides)).verified)
                intent, observation = contract["binding_intent"], contract["binding_observation"]
                binding = TaskBinding(provider=runner.provider, mode="visible", topology="visible-sdd",
                    issue_id="n1", role="developer", task_id=intent.task_id, host=intent.host_id,
                    worktree=intent.worktree, branch=intent.branch, run_id=run_id, generation=2,
                    allowlist=contract["files"], binding_intent=intent, binding_observation=observation)
                foreign = ProviderActionRunner(self.p.paths, runner.provider, "codex-app-visible")
                self.assertFalse(foreign.binding_gate(contract, binding).verified)
                context = contextvars.copy_context()
                results = []
                thread = threading.Thread(target=lambda: results.append(context.run(runtime_binding_gate, contract)))
                thread.start()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0].verified)
                checked.append(True)
            return original(contract, run_id, operation, request)
        with patch.object(runner, "_require_result", observe):
            self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(checked, [True])

    def test_scope_revoked_after_normal_and_exception_exit(self):
        import contextvars
        from vibe_guide.task_registry import runtime_binding_gate
        captures = []
        original = ProviderActionRunner.binding_gate
        def observe(runner, contract, binding):
            result = original(runner, contract, binding)
            if result.verified and binding.task_id == "protected-successor":
                captures.append((contextvars.copy_context(), dict(contract), binding))
            return result
        with patch.object(ProviderActionRunner, "binding_gate", observe):
            self.case.test_protected_visible_sdd_retirement_enters_normal_live_binding()
        self.assertTrue(captures)
        for context, contract, binding in captures:
            self.assertFalse(context.run(runtime_binding_gate, contract, binding).verified)

    def test_scope_revoked_after_unhandled_exception(self):
        import contextvars
        from vibe_guide.task_registry import runtime_binding_gate
        runner, state = self.ready_locate()
        captures = []
        original = runner._require_result
        def observe(contract, run_id, operation, request):
            if operation == "locate":
                captures.append((contextvars.copy_context(), dict(contract)))
                raise RuntimeError("injected scope exception")
            return original(contract, run_id, operation, request)
        with patch.object(runner, "_require_result", observe):
            with self.assertRaisesRegex(RuntimeError, "injected scope exception"):
                self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(len(captures), 1)
        context, contract = captures[0]
        self.assertFalse(context.run(runtime_binding_gate, contract).verified)

    def lock_boundary(self, boundary, inject=False):
        from vibe_guide.state import release_writer_lease, retirement_writer_lease
        runner, state = self.ready_locate()
        p = self.p
        # Advance only to the requested side-effect boundary.
        if boundary != "locate":
            state = p.monitor.tick(state.run_id, runner)
            locate, = [a for a in p.store.pending(state.run_id) if a["operation"] == "locate"]
            complete_native_mailbox_action(p.paths, state.run_id, locate["action_id"],
                {"structuredContent": {"located": True}}, request_digest=locate["request_digest"])
        if boundary in {"binding", "start"}:
            state = p.monitor.tick(state.run_id, runner)
            visible, = [a for a in p.store.pending(state.run_id) if a["operation"] == "visibility"]
            complete_native_mailbox_action(p.paths, state.run_id, visible["action_id"],
                {"structuredContent": {"visible": True, "direct_enter": True}},
                request_digest=visible["request_digest"])
        entered, finished = threading.Event(), threading.Event()
        outcomes, threads = [], []
        def begin_release():
            if threads:
                return
            held = retirement_writer_lease(p.paths, "n1", p.binding.worktree, state.run_id)
            def release():
                entered.set()
                try:
                    outcomes.append(release_writer_lease(p.paths, "n1", p.binding.worktree,
                                                        state.run_id, expected=held))
                except PermissionError:
                    outcomes.append("cas-rejected")
                finally:
                    finished.set()
            thread = threading.Thread(target=release)
            threads.append(thread)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(finished.wait(0.08), "release crossed writer fence")
            if inject:
                raise RuntimeError("injected fenced exception")
        if boundary in {"locate", "visibility"}:
            original = runner._action_owned
            def action(contract, run_id, operation, request, sequence=0):
                if operation == boundary:
                    begin_release()
                return original(contract, run_id, operation, request, sequence)
            target, name, hook = runner, "_action_owned", action
        elif boundary == "binding":
            import vibe_guide.monitor as module
            original = module.save_task_binding
            def save(*args, **kwargs):
                begin_release()
                return original(*args, **kwargs)
            target, name, hook = module, "save_task_binding", save
        else:
            original = runner._start_owned
            def start(*args, **kwargs):
                begin_release()
                return original(*args, **kwargs)
            target, name, hook = runner, "_start_owned", start
        try:
            with patch.object(target, name, hook):
                if inject:
                    with self.assertRaisesRegex(RuntimeError, "injected fenced exception"):
                        p.monitor.tick(state.run_id, runner)
                else:
                    result = p.monitor.tick(state.run_id, runner)
                    if boundary in {"binding", "start"}:
                        print("R22_FENCE", boundary, result.nodes["n1"].get("reason"), flush=True)
        finally:
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive(), "writer lock leaked after dispatch")
        self.assertTrue(threads)
        self.assertIn(outcomes, ([True], ["cas-rejected"]))

    def test_locate_holds_writer_fence_until_request_visible(self):
        self.lock_boundary("locate")

    def test_visibility_holds_writer_fence_until_request_visible(self):
        self.lock_boundary("visibility")

    def test_final_binding_holds_writer_fence(self):
        self.lock_boundary("binding")

    def test_start_holds_writer_fence(self):
        self.lock_boundary("start")

    def test_exception_releases_writer_fence(self):
        self.lock_boundary("locate", inject=True)

    def release_first(self, boundary):
        from vibe_guide.state import release_writer_lease, retirement_writer_lease
        runner, state = self.ready_locate()
        p = self.p
        for operation, payload in (("locate", {"located": True}),
                ("visibility", {"visible": True, "direct_enter": True})):
            state = p.monitor.tick(state.run_id, runner)
            action, = [a for a in p.store.pending(state.run_id) if a["operation"] == operation]
            complete_native_mailbox_action(p.paths, state.run_id, action["action_id"],
                {"structuredContent": payload}, request_digest=action["request_digest"])
        released = []
        handles = runner.store._directory("handles")
        before_handles = {path.name: path.read_bytes() for path in handles.glob("*.json")}
        def release():
            held = retirement_writer_lease(p.paths, "n1", p.binding.worktree, state.run_id)
            released.append(release_writer_lease(p.paths, "n1", p.binding.worktree, state.run_id, expected=held))
        if boundary == "binding":
            original = p.monitor._save_task_binding
            def hook(binding):
                release()
                return original(binding)
            target, name = p.monitor, "_save_task_binding"
        else:
            original = p.monitor._record
            def hook(snapshot, event, *args, **kwargs):
                if event == "start_intent":
                    release()
                return original(snapshot, event, *args, **kwargs)
            target, name = p.monitor, "_record"
        with patch.object(target, name, hook):
            p.monitor.tick(state.run_id, runner)
        self.assertEqual(released, [True])
        self.assertEqual({path.name: path.read_bytes() for path in handles.glob("*.json")}, before_handles)
        bound = load_task_binding(p.paths, "n1", "developer", run_id=state.run_id)
        if boundary == "binding":
            self.assertEqual(bound.task_id, p.binding.task_id)

    def test_release_before_final_binding_blocks_registry_and_handle(self):
        self.release_first("binding")

    def test_release_before_start_blocks_handle(self):
        self.release_first("start")


class ProtectedNoDowngradeTests(unittest.TestCase):
    def setUp(self):
        self.case = ProtectedFenceTests("test_active_scope_rejects_wrong_contract_runner_and_thread")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.p = self.case.p

    @staticmethod
    def downgraded(contract):
        altered = dict(contract)
        for key in ("binding_contract_version", "v39_binding", "binding_required",
                    "binding_intent", "binding_observation"):
            altered.pop(key, None)
        return altered

    def locate_downgrade(self, release_first=False):
        from vibe_guide.task_registry import runtime_binding_gate
        from vibe_guide.state import release_writer_lease, retirement_writer_lease
        runner, state = self.case.ready_locate()
        original, action = runner._require_result, runner._action
        observed, released = [], []
        def consume(contract, run_id, operation, request):
            if operation == "locate":
                altered = self.downgraded(contract)
                observed.append(runtime_binding_gate(altered).verified)
                return original(altered, run_id, operation, request)
            return original(contract, run_id, operation, request)
        def boundary(contract, run_id, operation, request, sequence=0):
            if release_first and operation == "locate":
                held = retirement_writer_lease(self.p.paths, "n1", self.p.binding.worktree, run_id)
                released.append(release_writer_lease(self.p.paths, "n1", self.p.binding.worktree,
                    run_id, expected=held))
            return action(contract, run_id, operation, request, sequence)
        with patch.object(runner, "_require_result", consume), patch.object(runner, "_action", boundary):
            self.p.monitor.tick(state.run_id, runner)
        if release_first:
            self.assertEqual(released, [True])
        self.assertFalse(any(a["operation"] == "locate" for a in self.p.store.pending(state.run_id)))
        self.assertEqual(observed, [False])

    def test_actual_locate_cannot_delete_protection(self):
        self.locate_downgrade()

    def test_actual_locate_cannot_downgrade_after_release(self):
        self.locate_downgrade(release_first=True)

    def test_active_scope_missing_flags_identity_and_evidence_fail_closed(self):
        from vibe_guide.task_registry import runtime_binding_gate
        runner, state = self.case.ready_locate()
        original = runner._require_result
        checked = []
        def consume(contract, run_id, operation, request):
            if operation == "locate":
                variants = [self.downgraded(contract)]
                for key in ("successor", "node_id", "role", "generation", "files",
                            "binding_intent", "binding_observation", "binding_contract_version"):
                    altered = dict(contract)
                    altered.pop(key, None)
                    variants.append(altered)
                variants.extend(dict(contract, **change) for change in (
                    {"binding_contract_version": False}, {"successor": False},
                    {"v39_binding": False, "binding_required": False, "binding_contract_version": False}))
                for altered in variants:
                    self.assertFalse(runtime_binding_gate(altered).verified, str(altered.keys()))
                    from vibe_guide.adapters.task_provider import ProviderUnavailable
                    with self.assertRaises(ProviderUnavailable):
                        runner._action(altered, run_id, "locate", {"threadId": "protected-successor"})
                checked.append(True)
            return original(contract, run_id, operation, request)
        with patch.object(runner, "_require_result", consume):
            self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(checked, [True])

    def completed_visibility(self):
        runner, state = self.case.ready_locate()
        for operation, payload in (("locate", {"located": True}),
                ("visibility", {"visible": True, "direct_enter": True})):
            state = self.p.monitor.tick(state.run_id, runner)
            action, = [a for a in self.p.store.pending(state.run_id) if a["operation"] == operation]
            complete_native_mailbox_action(self.p.paths, state.run_id, action["action_id"],
                {"structuredContent": payload}, request_digest=action["request_digest"])
        return runner, state

    def test_actual_visibility_deleted_successor_and_proof_cannot_enqueue(self):
        runner, state = self.case.ready_locate()
        state = self.p.monitor.tick(state.run_id, runner)
        locate, = [a for a in self.p.store.pending(state.run_id) if a["operation"] == "locate"]
        complete_native_mailbox_action(self.p.paths, state.run_id, locate["action_id"],
            {"structuredContent": {"located": True}}, request_digest=locate["request_digest"])
        original = runner._require_result
        def consume(contract, run_id, operation, request):
            if operation == "visibility":
                altered = self.downgraded(contract)
                altered.pop("successor", None)
                return original(altered, run_id, operation, request)
            return original(contract, run_id, operation, request)
        with patch.object(runner, "_require_result", consume):
            self.p.monitor.tick(state.run_id, runner)
        self.assertFalse(any(a["operation"] == "visibility" for a in self.p.store.pending(state.run_id)))

    def test_final_binding_cannot_downgrade_to_unverified_row(self):
        runner, state = self.completed_visibility()
        original = runner.task_binding
        def downgrade(*args, **kwargs):
            binding = original(*args, **kwargs)
            binding.binding_intent = None
            binding.binding_observation = None
            binding.binding_state = "blocked_unknown"
            binding.business_write_allowed = False
            return binding
        with patch.object(runner, "task_binding", downgrade):
            self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(load_task_binding(self.p.paths, "n1", "developer", run_id=state.run_id).task_id,
                         self.p.binding.task_id)
        self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])

    def test_start_deleted_protection_and_successor_cannot_write_handle(self):
        runner, state = self.completed_visibility()
        original = runner.start
        def consume(contract, root):
            altered = self.downgraded(contract)
            altered.pop("successor", None)
            return original(altered, root)
        with patch.object(runner, "start", consume):
            self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])


class ProtectedRowIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = ProtectedNoDowngradeTests("test_actual_locate_cannot_delete_protection")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.p = self.case.p

    def final_row_drift(self, downgrade=False):
        runner, state = self.case.completed_visibility()
        original = self.p.monitor._save_task_binding
        attempts = []
        def drift(binding):
            if binding.task_id == "protected-successor" and not attempts:
                attempts.append(binding.generation)
                binding.generation = 999
                if downgrade:
                    binding.binding_state = "blocked_unknown"
                    binding.business_write_allowed = False
            return original(binding)
        with patch.object(self.p.monitor, "_save_task_binding", drift):
            result = self.p.monitor.tick(state.run_id, runner)
        bound = load_task_binding(self.p.paths, "n1", "developer", run_id=state.run_id)
        self.assertEqual(attempts, [self.p.binding.generation + 1])
        self.assertEqual(bound.task_id, self.p.binding.task_id)
        self.assertEqual(bound.generation, self.p.binding.generation)
        self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])
        self.assertIsNone(result.nodes["n1"].get("active_task"))

    def test_final_row_generation_drift_rejected_before_save(self):
        self.final_row_drift()

    def test_final_row_generation_and_state_drift_rejected_before_save(self):
        self.final_row_drift(downgrade=True)

    @staticmethod
    def invalid_rows(binding):
        for field, value in (
                ("run_id", "other-run"), ("issue_id", "other-node"), ("role", "reviewer"),
                ("provider", "other-provider"), ("mode", "background"), ("topology", "dual-visible"),
                ("task_id", "other-task"), ("task_id", None), ("host", "other-host"), ("host", None),
                ("generation", 999), ("generation", None), ("successor_of", None),
                ("worktree", "/other-root"), ("worktree", None), ("branch", "other-branch"),
                ("allowlist", ["foreign.py"]), ("allowlist", []),
                ("capability_contract_digest", "other-capability"),
                ("binding_intent", None), ("binding_observation", None)):
            row = copy.copy(binding)
            setattr(row, field, value)
            yield field, row
        row = copy.copy(binding)
        row.generation = 999
        row.binding_state = "blocked_unknown"
        row.business_write_allowed = False
        yield "generation-and-state", row

    def test_actual_final_row_identity_matrix_has_zero_side_effects(self):
        runner, state = self.case.completed_visibility()
        original = self.p.monitor._save_task_binding
        checked = []
        def consume(binding):
            if binding.task_id == "protected-successor" and not checked:
                for field, row in self.invalid_rows(binding):
                    with self.subTest(field=field):
                        with self.assertRaises((ValueError, TypeError)):
                            original(row)
                        bound = load_task_binding(self.p.paths, "n1", "developer", run_id=state.run_id)
                        self.assertEqual(bound.task_id, self.p.binding.task_id)
                        self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])
                checked.append(True)
            return original(binding)
        with patch.object(self.p.monitor, "_save_task_binding", consume):
            result = self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(checked, [True])
        self.assertEqual(result.nodes["n1"]["active_task"]["task_id"], "protected-successor")
        self.assertIn("n1", result.handles)

    def test_registry_reload_identity_matrix_blocks_before_handle(self):
        import vibe_guide.runners.provider_action as module
        runner, state = self.case.completed_visibility()
        original = runner.start
        load = module.load_task_binding
        checked = []
        def consume(contract, root):
            binding = load(self.p.paths, "n1", "developer", run_id=state.run_id)
            # start legitimately refreshes typed evidence from the live
            # contract; all persisted identity fields still need to match.
            for field, row in self.invalid_rows(binding):
                if field in {"binding_intent", "binding_observation"}:
                    continue
                with self.subTest(field=field), patch.object(module, "load_task_binding", return_value=row):
                    with self.assertRaises((ValueError, TypeError, RuntimeError)):
                        original(contract, root)
                    self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])
            checked.append(True)
            return original(contract, root)
        with patch.object(runner, "start", consume):
            result = self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(checked, [True])
        self.assertIn("n1", result.handles)


class ProtectedCanonicalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = ProtectedRowIdentityTests("test_final_row_generation_drift_rejected_before_save")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.p = self.case.p

    def final_identity_drift(self, field, value):
        runner, state = self.case.case.completed_visibility()
        original = self.p.monitor._save_task_binding
        seen = []
        def drift(binding):
            if binding.task_id == "protected-successor" and not seen:
                seen.append(binding.task_id)
                setattr(binding, field, value)
            return original(binding)
        with patch.object(self.p.monitor, "_save_task_binding", drift):
            result = self.p.monitor.tick(state.run_id, runner)
        bound = load_task_binding(self.p.paths, "n1", "developer", run_id=state.run_id)
        self.assertEqual(seen, ["protected-successor"])
        self.assertEqual(bound.task_id, self.p.binding.task_id)
        self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])
        self.assertIsNone(result.nodes["n1"].get("active_task"))

    def test_final_thread_alias_drift_rejected_before_registry(self):
        self.final_identity_drift("threadId", "foreign-thread-r24")

    def test_final_cursor_drift_rejected_before_registry(self):
        self.final_identity_drift("cursor", "foreign-cursor-r24")

    @staticmethod
    def conflicting_rows(binding):
        for field, value in (("threadId", "foreign-thread"),
                             ("platform_task_id", "foreign-platform-task"),
                             ("hostId", "foreign-host"),
                             ("cursor", "foreign-cursor"),
                             ("visible", False)):
            row = copy.copy(binding)
            setattr(row, field, value)
            yield field, row

    def test_final_normalization_matrix_rejects_before_save_and_keeps_live_cursor(self):
        runner, state = self.case.case.completed_visibility()
        original = self.p.monitor._save_task_binding
        checked = []
        def consume(binding):
            if binding.task_id == "protected-successor" and not checked:
                for field, row in self.conflicting_rows(binding):
                    with self.subTest(field=field):
                        with self.assertRaises((ValueError, TypeError)):
                            original(row)
                        bound = load_task_binding(self.p.paths, "n1", "developer", run_id=state.run_id)
                        self.assertEqual(bound.task_id, self.p.binding.task_id)
                        self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])
                self.assertIsNone(binding.cursor)
                binding.cursor = binding.binding_observation.cursor
                checked.append(binding.cursor)
            return original(binding)
        with patch.object(self.p.monitor, "_save_task_binding", consume):
            result = self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(checked, ["protected-cursor"])
        self.assertIn("n1", result.handles)
        bound = load_task_binding(self.p.paths, "n1", "developer", run_id=state.run_id)
        self.assertEqual(bound.cursor, checked[0])
        handle, = list(runner.store._directory("handles").glob("*.json"))
        self.assertEqual(runner.store._read(handle)["cursor"], checked[0])

    def test_registry_reload_normalization_matrix_has_zero_handles(self):
        import vibe_guide.runners.provider_action as module
        runner, state = self.case.case.completed_visibility()
        original = runner.start
        load = module.load_task_binding
        checked = []
        def consume(contract, root):
            binding = load(self.p.paths, "n1", "developer", run_id=state.run_id)
            before = binding.to_dict()
            for field, row in self.conflicting_rows(binding):
                with self.subTest(field=field), patch.object(module, "load_task_binding", return_value=row):
                    with self.assertRaises((ValueError, TypeError, RuntimeError)):
                        original(contract, root)
                    self.assertEqual(list(runner.store._directory("handles").glob("*.json")), [])
                    self.assertEqual(load(self.p.paths, "n1", "developer", run_id=state.run_id).to_dict(), before)
            checked.append(True)
            self.assertIsNone(binding.cursor)
            return original(contract, root)
        with patch.object(runner, "start", consume):
            result = self.p.monitor.tick(state.run_id, runner)
        self.assertEqual(checked, [True])
        self.assertIn("n1", result.handles)
        self.assertEqual(result.nodes["n1"]["active_task"]["task_id"], "protected-successor")


class ReceiptConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.probe = AuthoritySourceTests("test_missing_dispatch_does_not_issue_wait")
        self.probe.setUp()
        self.addCleanup(self.probe.doCleanups)

    def terminal_wait(self):
        p = self.probe
        runner, handle, resume = p.mailbox_start_interruption()
        p.finish_resume(resume)
        p.monitor.tick(p.snapshot.run_id, runner)
        wait, = [a for a in p.store.pending(p.snapshot.run_id) if a["operation"] == "wait"]
        p.store.complete(wait["action_id"], {"status": "stopped", "event": "stopped",
            "cursor": "r21-terminal-cursor"}, request_digest=wait["request_digest"])
        return runner, handle

    def frozen(self, runner, handle):
        p = self.probe
        return (runner._handle_path(handle.run_id).read_bytes(),
            load_task_binding(p.paths, "n1", "developer", run_id=p.snapshot.run_id).cursor,
            {str(path.relative_to(p.store.root)): path.read_bytes()
             for path in p.store._directory("requests").rglob("*.json")})

    def test_public_tick_poll_bad_source_must_not_consume_terminal(self):
        p = self.probe
        runner, handle = self.terminal_wait()
        path = runner._handle_path(handle.run_id)
        metadata = runner.store._read(path)
        metadata["intent_digest"] = "corrupt-source"
        runner.store._atomic(path, metadata)
        before = self.frozen(runner, handle)
        p.monitor.tick(p.snapshot.run_id, runner)
        self.assertEqual(self.frozen(runner, handle), before)

    def test_public_tick_takeover_before_poll_must_not_consume_terminal(self):
        p = self.probe
        runner, handle = self.terminal_wait()
        before = self.frozen(runner, handle)
        original = runner.poll
        epochs = []
        def raced(handle):
            epochs.append(p.lease.takeover(p.snapshot.run_id, p.epoch,
                owner={"session_id": "r21-next-owner", "provider": "codex"}))
            return original(handle)
        with patch.object(runner, "poll", side_effect=raced):
            try:
                p.monitor.tick(p.snapshot.run_id, runner)
            except PermissionError:
                pass
        self.assertEqual(epochs, [p.epoch + 1])
        self.assertEqual(self.frozen(runner, handle), before)

    def test_direct_resume_bad_source_preserves_pending(self):
        p = self.probe
        runner, handle, resume = p.mailbox_start_interruption()
        p.finish_resume(resume)
        path = runner._handle_path(handle.run_id)
        metadata = runner.store._read(path)
        metadata["intent_digest"] = "corrupt-resume"
        runner.store._atomic(path, metadata)
        before = self.frozen(runner, handle)
        with self.assertRaises(PermissionError):
            runner.poll(handle)
        self.assertEqual(self.frozen(runner, handle), before)

    def test_direct_terminal_poll_bad_source_is_read_only(self):
        runner, handle = self.terminal_wait()
        runner.stop(handle)
        path = runner._handle_path(handle.run_id)
        metadata = runner.store._read(path)
        metadata["intent_digest"] = "corrupt-terminal"
        runner.store._atomic(path, metadata)
        before = self.frozen(runner, handle)
        with self.assertRaises(PermissionError):
            runner.poll(handle)
        self.assertEqual(self.frozen(runner, handle), before)

    def test_new_owner_public_tick_consumes_valid_terminal(self):
        p = self.probe
        runner, handle = self.terminal_wait()
        p.lease.takeover(p.snapshot.run_id, p.epoch,
            owner={"session_id": "r21-next-owner", "provider": "codex"})
        p.monitor.tick(p.snapshot.run_id, runner)
        metadata = runner.store._read(runner._handle_path(handle.run_id))
        self.assertTrue(metadata["terminal_confirmed"])
        self.assertIsNone(metadata["pending_action"])
        self.assertEqual(metadata["cursor"], "r21-terminal-cursor")

    def test_new_owner_timeout_rearms_wait_without_nested_lock(self):
        p = self.probe
        runner, handle = p.waiting_handle()
        p.lease.takeover(p.snapshot.run_id, p.epoch,
            owner={"session_id": "r21-next-owner", "provider": "codex"})
        p.monitor.tick(p.snapshot.run_id, runner)
        metadata = runner.store._read(runner._handle_path(handle.run_id))
        self.assertEqual(metadata["pending_operation"], "wait")
        self.assertEqual(metadata["wait_sequence"], 2)

    def test_poll_lock_first_blocks_takeover_until_consumption_returns(self):
        p = self.probe
        runner, handle = self.terminal_wait()
        original = runner.store.result
        attempting, finished = threading.Event(), threading.Event()
        epochs, threads = [], []
        def takeover():
            attempting.set()
            try:
                epochs.append(p.lease.takeover(p.snapshot.run_id, p.epoch,
                    owner={"session_id": "r21-next-owner", "provider": "codex"}))
            finally:
                finished.set()
        def observed(action_id):
            thread = threading.Thread(target=takeover)
            threads.append(thread)
            thread.start()
            self.assertTrue(attempting.wait(2))
            self.assertFalse(finished.wait(0.05))
            return original(action_id)
        with patch.object(runner.store, "result", side_effect=observed):
            events = runner.poll(handle)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(epochs, [p.epoch + 1])
        self.assertEqual(events[0].event, "stopped")

    def test_poll_exception_releases_lock(self):
        p = self.probe
        runner, handle = self.terminal_wait()
        with patch.object(runner.store, "result", side_effect=RuntimeError("receipt unavailable")):
            with self.assertRaises(RuntimeError):
                runner.poll(handle)
        epochs = []
        thread = threading.Thread(target=lambda: epochs.append(p.lease.takeover(
            p.snapshot.run_id, p.epoch, owner={"session_id": "r21-next-owner", "provider": "codex"})))
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(epochs, [p.epoch + 1])


class StopAuthorityTests(AuthoritySourceTests):
    def check_completed_delivery_transition(self, takeover=False):
        from vibe_guide.state import save_snapshot
        runner, handle, action = self.mailbox_start_interruption()
        self.finish_resume(action)
        state = self.monitor.tick(self.snapshot.run_id, runner)
        current = state.nodes["n1"]
        binding = load_task_binding(self.paths, "n1", "developer", run_id=state.run_id)
        root = self.monitor._worktree_path(current)
        root.mkdir(parents=True, exist_ok=True)
        (root / "r20-delivery.md").write_text("R20_DELIVERY_COMPLETE")
        current["worktree"] = binding.worktree
        current["branch"] = binding.branch
        current["delivery_evidence"] = {"completion_marker": "R20_DELIVERY_COMPLETE",
            "delivery_path": str(root / "r20-delivery.md"), "thread_status": "complete"}
        save_snapshot(self.paths, state)
        nodes = copy.deepcopy(list(self.monitor.nodes.values()))
        record = authorize(build_authorization_card(self.monitor.plan, nodes,
            self.fixture.capabilities, active_pair_limit=2), "AUTHORIZE")
        renewed = Monitor(self.paths, self.monitor.plan, nodes)
        original = renewed._reconcile_completed_delivery_for_transition
        before = binding.to_dict()
        epochs = []
        def observed(*args):
            result = original(*args)
            self.assertTrue(result)
            if takeover:
                epochs.append(self.lease.takeover(state.run_id, self.epoch,
                    owner={"session_id": "next-owner", "provider": "codex"}))
            return result
        with patch.object(renewed, "_reconcile_completed_delivery_for_transition", side_effect=observed), \
                patch.object(runner, "stop", wraps=runner.stop) as stop:
            if takeover:
                with self.assertRaises(PermissionError):
                    renewed.reauthorize(state.run_id, record, runner, "executable_contract_changed")
            else:
                renewed.reauthorize(state.run_id, record, runner, "executable_contract_changed")
        self.assertEqual(stop.call_count, 0)
        if takeover:
            self.assertEqual(epochs, [self.epoch + 1])
            self.assertEqual(load_task_binding(self.paths, "n1", "developer", run_id=state.run_id).to_dict(), before)
            self.assertIn("n1", load_snapshot(self.paths, state.run_id).handles)

    def test_completed_delivery_uses_no_stop(self):
        self.check_completed_delivery_transition()

    def test_completed_delivery_takeover_preserves_binding(self):
        self.check_completed_delivery_transition(True)

    def test_stop_and_notice_identity_damage_is_read_only(self):
        for method in ("stop", "send_notice"):
            for field, value in (("run_id", "run-r20-unknown"), ("node_id", "other"),
                    ("role", "reviewer"), ("generation", 88), ("task_id", "other"),
                    ("host", "other-host"), ("handle_id", "bridge-other")):
                with self.subTest(method=method, field=field):
                    probe = StopAuthorityTests("test_live_stop_must_validate_handle_host_source")
                    probe.setUp()
                    try:
                        runner, handle = probe.waiting_handle()
                        path = runner._handle_path(handle.run_id)
                        metadata = json.loads(path.read_text())
                        metadata[field] = value
                        path.write_text(json.dumps(metadata))
                        before = {str(p.relative_to(probe.store.root)): p.read_bytes()
                            for p in probe.store.root.rglob("*.json")}
                        binding = load_task_binding(probe.paths, "n1", "developer",
                            run_id=probe.snapshot.run_id).to_dict()
                        with self.assertRaises((PermissionError, KeyError, ValueError, FileNotFoundError)):
                            getattr(runner, method)(handle, "notice") if method == "send_notice" else runner.stop(handle)
                        self.assertEqual({str(p.relative_to(probe.store.root)): p.read_bytes()
                            for p in probe.store.root.rglob("*.json")}, before)
                        self.assertEqual(load_task_binding(probe.paths, "n1", "developer",
                            run_id=probe.snapshot.run_id).to_dict(), binding)
                    finally:
                        probe.doCleanups()

    def test_new_owner_can_reconcile_valid_stop(self):
        runner, handle, action = self.mailbox_start_interruption()
        self.finish_resume(action)
        self.monitor.tick(self.snapshot.run_id, runner)
        wait, = [a for a in self.store.pending(self.snapshot.run_id) if a["operation"] == "wait"]
        self.store.complete(wait["action_id"], {"status": "stopped", "event": "stopped", "cursor": "r20-stop"})
        self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "next-owner", "provider": "codex"})
        nodes = copy.deepcopy(list(self.monitor.nodes.values()))
        record = authorize(build_authorization_card(self.monitor.plan, nodes,
            self.fixture.capabilities, active_pair_limit=2), "AUTHORIZE")
        renewed = Monitor(self.paths, self.monitor.plan, nodes)
        renewed.reauthorize(self.snapshot.run_id, record, runner, "executable_contract_changed")
        self.assertTrue(json.loads(runner._handle_path(handle.run_id).read_text())["terminal_confirmed"])

    def test_live_stop_must_validate_handle_host_source(self):
        runner, handle, action = self.mailbox_start_interruption()
        self.finish_resume(action)
        self.monitor.tick(self.snapshot.run_id, runner)
        wait, = [a for a in self.store.pending(self.snapshot.run_id)
            if a['operation'] == 'wait']
        self.store.complete(wait['action_id'], {'status': 'stopped', 'event': 'stopped',
            'cursor': 'reviewer-r19-source-cursor'})
        path = runner._handle_path(handle.run_id)
        metadata = json.loads(path.read_text())
        metadata['host'] = 'unverified-other-host'
        path.write_text(json.dumps(metadata))
        nodes = copy.deepcopy(list(self.monitor.nodes.values()))
        record = authorize(build_authorization_card(self.monitor.plan, nodes,
            self.fixture.capabilities, active_pair_limit=2), 'AUTHORIZE')
        renewed = Monitor(self.paths, self.monitor.plan, nodes)
        original = renewed._set_binding_status
        stopped = []
        def observed(snapshot, node_id, role, status):
            if status == 'stopped':
                stopped.append(True)
            return original(snapshot, node_id, role, status)
        with patch.object(renewed, '_set_binding_status', side_effect=observed):
            try:
                renewed.reauthorize(self.snapshot.run_id, record, runner, 'executable_contract_changed')
            except (PermissionError, RuntimeError):
                pass
        print('STOP_SOURCE', 'stopped_registry_writes', len(stopped),
            'corrupt_host', metadata['host'], 'original_host', self.store.action(wait['action_id'])['request']['targets'][0]['hostId'], flush=True)
        self.assertEqual(stopped, [], 'live stop trusted conflicting handle host and bypassed poll source gate')

    def test_live_takeover_before_stop_must_not_consume_or_stop(self):
        runner, handle, action = self.mailbox_start_interruption()
        self.finish_resume(action)
        self.monitor.tick(self.snapshot.run_id, runner)
        wait, = [a for a in self.store.pending(self.snapshot.run_id)
            if a['operation'] == 'wait']
        self.store.complete(wait['action_id'], {'status': 'stopped', 'event': 'stopped',
            'cursor': 'reviewer-r19-stopped-cursor'})
        nodes = copy.deepcopy(list(self.monitor.nodes.values()))
        record = authorize(build_authorization_card(self.monitor.plan, nodes,
            self.fixture.capabilities, active_pair_limit=2), 'AUTHORIZE')
        renewed = Monitor(self.paths, self.monitor.plan, nodes)
        original = renewed._reconcile_completed_delivery_for_transition
        epochs = []
        binding_before = load_task_binding(self.paths, 'n1', 'developer',
            run_id=self.snapshot.run_id).to_dict()
        metadata_before = runner._handle_path(handle.run_id).read_bytes()
        def raced(*args):
            value = original(*args)
            self.assertFalse(value)
            self.assertEqual(args[0]._dispatch_owner['lease_epoch'], self.epoch)
            epochs.append(self.lease.takeover(self.snapshot.run_id, self.epoch,
                owner={'session_id': 'next-owner', 'provider': 'codex'}))
            return value
        with patch.object(renewed, '_reconcile_completed_delivery_for_transition', side_effect=raced), \
             patch.object(runner, 'stop', wraps=runner.stop) as stop:
            try:
                renewed.reauthorize(self.snapshot.run_id, record, runner, 'executable_contract_changed')
            except PermissionError:
                pass
        binding_after = load_task_binding(self.paths, 'n1', 'developer',
            run_id=self.snapshot.run_id).to_dict()
        metadata_after = json.loads(runner._handle_path(handle.run_id).read_text())
        print('LIVE_PRE_STOP', 'takeover', epochs, 'stop_calls', stop.call_count,
            'binding', (binding_before['status'], binding_after['status']),
            'cursor', (binding_before['cursor'], binding_after['cursor']),
            'terminal_confirmed', metadata_after['terminal_confirmed'],
            'new_authority', load_snapshot(self.paths, self.snapshot.run_id).authorization_digest == record.digest, flush=True)
        self.assertEqual(epochs, [self.epoch + 1])
        self.assertEqual(stop.call_count, 0, 'stale live transition invoked real stop after takeover')
        self.assertEqual(binding_after, binding_before)
        self.assertEqual(runner._handle_path(handle.run_id).read_bytes(), metadata_before)
