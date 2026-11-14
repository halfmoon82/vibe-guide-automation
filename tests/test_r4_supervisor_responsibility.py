"""Adopted R4 faults belong to the registered supervisor, never a parallel LLM."""
import unittest
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_daemon_heal import Base, FakeSupervisor, NODE, RUN, stalled
from vibe_guide import daemon as dm
from vibe_guide.supervisor import _beat_extras
from tests import support_vibe_home as hermetic

setUpModule, tearDownModule = hermetic.module_hooks()


class SupervisorHelpTransactionTests(unittest.TestCase):
    def setUp(self):
        from tests import test_monitor as fixture
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.state import run_dir
        from vibe_guide.supervisor import SupervisorLease
        self.fixture = fixture.MonitorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.paths = self.fixture.paths
        from dataclasses import replace
        self.fixture.capabilities = replace(self.fixture.capabilities, agent_id="codex")
        monitor, authorization = self.fixture.authorized_monitor([fixture.node("n1")])
        self.monitor = monitor
        self.snapshot = monitor.start(authorization, FakeRunner())
        self.lease = SupervisorLease(self.paths)
        self.epoch = self.lease.takeover(self.snapshot.run_id, 0,
            owner={"session_id": "original-supervisor", "provider": "codex"})
        rules = patch("vibe_guide.protocol_adoption.effective_responsibility",
                      return_value={"status": "effective"})
        rules.start()
        self.addCleanup(rules.stop)
        self.file = run_dir(self.paths, self.snapshot.run_id) / "heal-state.json"
        self.file.write_text(json.dumps({"nodes": {"n1": {"supervisor_responsibility": {
            "status": "pending", "node_id": "n1", "role": "developer",
            "authorization_digest": self.snapshot.authorization_digest,
            "node_contract_digest": self.snapshot.node_contract_digest}}}}))
        evidence = self.paths.root / "diagnosis.md"
        evidence.write_text("original command rejected unsupported option; supported usage observed")
        self.help = {"diagnosis": "wrong invocation, capability is present",
                     "action": "use the supported option in the original task",
                     "next_action": "original worker retries the failed command",
                     "verification_condition": "original command exits zero with expected artifact",
                     "evidence_ref": "diagnosis.md",
                     "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}

    def test_current_supervisor_help_is_durable_idempotent_and_not_resolution(self):
        from vibe_guide import supervisor
        first = supervisor.record_supervisor_help(self.paths, self.snapshot.run_id, "n1",
            self.help, owner_epoch=self.epoch, owner_session_id="original-supervisor")
        before = self.file.read_bytes()
        second = supervisor.record_supervisor_help(self.paths, self.snapshot.run_id, "n1",
            self.help, owner_epoch=self.epoch, owner_session_id="original-supervisor")
        self.assertEqual(first["help_digest"], second["help_digest"])
        self.assertEqual(self.file.read_bytes(), before)
        pending = supervisor.pending_supervisor_responsibilities(self.paths, self.snapshot.run_id)[0]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(len(pending["attempted_actions"]), 1)
        self.assertEqual(pending["next_action"], self.help["next_action"])

    def test_reported_engineering_block_can_receive_help_on_original_binding(self):
        from vibe_guide.supervisor import record_supervisor_help
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.task_registry import load_task_binding
        runner = FakeRunner(events={("n1", "developer"): [("blocked_engineering", {
            "status": "blocked", "implementation_complete": False,
            "failed_step": "original command failed", "missing_conditions": ["supported syntax"]})]})
        # Restore the existing handle mapping on this runner; no new dispatch.
        handle = self.snapshot.handles["n1"]
        runner._nodes_by_handle[handle] = "n1"
        runner._roles_by_handle[handle] = "developer"
        runner._claims_by_handle[handle] = {"node_id": "n1",
            **self.snapshot.nodes["n1"]["active_task"]}
        blocked = self.monitor.tick(self.snapshot.run_id, runner)
        self.assertIsNone(blocked.nodes["n1"]["active_task"])
        result = record_supervisor_help(self.paths, self.snapshot.run_id, "n1", self.help,
            owner_epoch=self.epoch, owner_session_id="original-supervisor")
        self.assertEqual(result["status"], "pending")
        original = load_task_binding(self.paths, "n1", "developer", run_id=self.snapshot.run_id)
        help_record = json.loads(self.file.read_text())["nodes"]["n1"]["supervisor_responsibility"]["attempted_actions"][-1]
        self.assertEqual(help_record["task_id"], original.task_id)
        self.assertEqual(help_record["generation"], original.generation)
        from vibe_guide.supervisor import _node_role
        from vibe_guide.state import load_events, save_snapshot
        events = load_events(self.paths, self.snapshot.run_id)
        self.assertEqual(_node_role(blocked.nodes["n1"], events=events, node_id="n1"), "developer")
        reviewer_event = {"event": "blocked_engineering", "data": {"node_id": "n1"},
                          "provenance": {"role": "reviewer"}}
        self.assertEqual(_node_role(blocked.nodes["n1"], events=[reviewer_event], node_id="n1"), "reviewer")
        before = self.file.read_bytes()
        # The registry alone cannot authorize a stale or different writer.
        for key, value in (("developer_identity", "replacement-task"),
                           ("developer_generation", original.generation + 1),
                           ("worktree", ".worktrees/another-writer"),
                           ("branch", "another-branch")):
            with self.subTest(changed=key):
                old = blocked.nodes["n1"][key]
                blocked.nodes["n1"][key] = value
                save_snapshot(self.paths, blocked)
                with self.assertRaises((PermissionError, ValueError)):
                    record_supervisor_help(self.paths, self.snapshot.run_id, "n1", self.help,
                        owner_epoch=self.epoch, owner_session_id="original-supervisor")
                self.assertEqual(self.file.read_bytes(), before)
                blocked.nodes["n1"][key] = old
                save_snapshot(self.paths, blocked)
        from vibe_guide.task_registry import save_task_binding
        original.status = "archived"
        save_task_binding(self.paths, original)
        with self.assertRaises(PermissionError):
            record_supervisor_help(self.paths, self.snapshot.run_id, "n1", self.help,
                owner_epoch=self.epoch, owner_session_id="original-supervisor")
        self.assertEqual(self.file.read_bytes(), before)

    def test_stale_owner_or_changed_evidence_cannot_replace_help(self):
        from vibe_guide import supervisor
        for kind in ("owner", "evidence"):
            with self.subTest(kind=kind):
                value = dict(self.help)
                if kind == "evidence":
                    value["evidence_sha256"] = "0" * 64
                before = self.file.read_bytes()
                with self.assertRaises(PermissionError):
                    supervisor.record_supervisor_help(self.paths, self.snapshot.run_id, "n1", value,
                        owner_epoch=self.epoch, owner_session_id="stale" if kind == "owner" else "original-supervisor")
                self.assertEqual(self.file.read_bytes(), before)

    def test_public_mailbox_help_uses_actual_registered_supervisor(self):
        from vibe_guide.cli import run_cli
        source = self.paths.root / "help.json"
        source.write_text(json.dumps(self.help))
        args = ["supervisor-mailbox", "--run-id", self.snapshot.run_id,
                "--node", "n1", "--record-help", "--payload", str(source), "--json"]
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "original-supervisor"}, clear=True):
            result = run_cli(args, self.paths.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["status"], "pending")
        before = self.file.read_bytes()
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "stale"}, clear=True):
            result = run_cli(args, self.paths.root)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.file.read_bytes(), before)

    def test_adopted_dead_daemon_is_current_supervisor_work_not_idle(self):
        from vibe_guide.supervisor import _beat_extras
        payload = {"state": "idle", "end_turn": True}
        # No node pending: daemon failure itself must keep responsibility.
        self.file.write_text(json.dumps({"nodes": {}}))
        text = _beat_extras(self.paths, self.snapshot.run_id, payload, "idle")
        self.assertEqual(payload["state"], "work")
        self.assertFalse(payload["end_turn"])
        self.assertIn("进程", text)
        self.assertNotIn("只上报，不重启", text)

    def test_daemon_command_pins_product_runtime_despite_shadow_package_for_two_processes(self):
        import subprocess
        shadow = self.paths.root / "vibe_guide"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("raise RuntimeError('business shadow package loaded')")
        (shadow / "__main__.py").write_text("raise RuntimeError('business shadow entry loaded')")
        frozen = {path.name: path.read_bytes() for path in shadow.iterdir()}
        command = dm.build_run_command(self.snapshot.run_id, self.snapshot.plan_id)
        self.assertEqual(command[1], "-c")
        for _ in range(2):
            result = subprocess.run(command[:3] + ["--help"], cwd=self.paths.root,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("supervisor-mailbox", result.stdout)
        self.assertEqual({path.name: path.read_bytes() for path in shadow.iterdir()}, frozen)

    def test_alive_stale_daemon_cannot_spawn_second_process(self):
        import os
        from vibe_guide.state import run_dir
        directory = run_dir(self.paths, self.snapshot.run_id)
        (directory / dm.DAEMON_FILE).write_text(json.dumps({"pid": os.getpid(), "state": "running",
            "heartbeat": 1, "stale_seconds": 1, "cycles": 2}))
        calls = []
        result = dm.start_daemon(self.paths, self.snapshot.run_id, self.snapshot.plan_id,
            spawn=lambda *args, **kwargs: calls.append((args, kwargs)), wait=0)
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "start_unknown")
        self.assertIn("process", result["reason"])

    def test_unregistered_spawn_remains_claimed_across_second_start(self):
        import os
        calls = []
        def spawn(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        first = dm.start_daemon(self.paths, self.snapshot.run_id, self.snapshot.plan_id,
                                spawn=spawn, wait=0)
        self.assertEqual(first["status"], "start_unknown")
        second = dm.start_daemon(self.paths, self.snapshot.run_id, self.snapshot.plan_id,
                                 spawn=spawn, wait=0)
        self.assertEqual(second["status"], "start_unknown")
        self.assertEqual(len(calls), 1)

    def test_process_probe_error_does_not_become_absent_before_start(self):
        from vibe_guide.state import run_dir
        (run_dir(self.paths, self.snapshot.run_id) / dm.DAEMON_FILE).write_text(json.dumps(
            {"pid": 12345678, "state": "exited", "heartbeat": 1}))
        calls = []
        with patch("os.kill", side_effect=OSError("process query unknown")):
            result = dm.start_daemon(self.paths, self.snapshot.run_id, self.snapshot.plan_id,
                spawn=lambda *args, **kwargs: calls.append((args, kwargs)), wait=0)
        self.assertEqual(result["status"], "start_unknown")
        self.assertEqual(calls, [])

    def test_daemon_recovery_requires_original_owner_and_current_authorized_runtime(self):
        from vibe_guide import supervisor
        calls = []
        def starter(paths, run_id, plan_id, **kwargs):
            calls.append((run_id, plan_id, kwargs))
            return {"status": "start_unknown", "reason": "awaiting two verified cycles"}
        with self.assertRaises(PermissionError):
            supervisor.recover_daemon(self.paths, self.snapshot.run_id,
                owner_epoch=self.epoch, owner_session_id="old-owner", starter=starter)
        self.assertEqual(calls, [])
        result = supervisor.recover_daemon(self.paths, self.snapshot.run_id,
            owner_epoch=self.epoch, owner_session_id="original-supervisor", starter=starter)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], (self.snapshot.run_id, self.snapshot.plan_id))
        self.assertEqual(calls[0][2]["provider"], "codex")
        # Recovery still holds the owner lock: the spawned daemon must not
        # be awaited while it needs that same lock to register its lease.
        self.assertEqual(calls[0][2].get("wait"), 0)
        self.assertEqual(calls[0][2]["recovery_owner"], {
            "epoch": self.epoch, "session_id": "original-supervisor",
            "authorization_digest": self.snapshot.authorization_digest,
            "node_contract_digest": self.snapshot.node_contract_digest})
        self.assertEqual(result["status"], "start_unknown")

    def test_current_owner_cannot_recover_daemon_on_another_platform(self):
        from vibe_guide.supervisor import recover_daemon
        self.epoch = self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "original-supervisor", "provider": "workbuddy"})
        calls = []
        with self.assertRaisesRegex(PermissionError, "provider"):
            recover_daemon(self.paths, self.snapshot.run_id, owner_epoch=self.epoch,
                owner_session_id="original-supervisor",
                starter=lambda *args, **kwargs: (calls.append((args, kwargs)) or {"status": "start_unknown"}))
        self.assertEqual(calls, [])

    def test_only_claimed_recovery_child_can_acquire_live_owner_lease(self):
        import os
        import json
        from vibe_guide.state import run_dir
        from vibe_guide.supervisor import SupervisorLease
        child_pid = os.getpid() + 100000
        intent = {"run_id": self.snapshot.run_id, "plan_id": self.snapshot.plan_id,
            "status": "spawned", "pid": child_pid,
            "recovery_owner": {"epoch": self.epoch, "session_id": "original-supervisor",
                "authorization_digest": self.snapshot.authorization_digest,
                "node_contract_digest": self.snapshot.node_contract_digest}}
        path = run_dir(self.paths, self.snapshot.run_id) / "daemon-start.json"
        path.write_text(json.dumps(intent))
        wrong = SupervisorLease(self.paths, pid=child_pid + 1)
        self.assertFalse(wrong.acquire(self.snapshot.run_id))
        child = SupervisorLease(self.paths, pid=child_pid)
        self.assertTrue(child.acquire(self.snapshot.run_id))
        record = json.loads(self.lease.path(self.snapshot.run_id).read_text())
        self.assertEqual(record["pid"], child_pid)
        self.assertEqual(record["lease_epoch"], self.epoch)
        self.assertEqual(record["owner"]["session_id"], "original-supervisor")

    def test_claimed_child_rejects_owner_on_unauthorized_platform(self):
        import os
        from vibe_guide.state import run_dir
        from vibe_guide.supervisor import SupervisorLease
        self.epoch = self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "original-supervisor", "provider": "workbuddy"})
        child_pid = os.getpid() + 100000
        intent = {"run_id": self.snapshot.run_id, "plan_id": self.snapshot.plan_id,
            "status": "spawned", "pid": child_pid,
            "recovery_owner": {"epoch": self.epoch, "session_id": "original-supervisor",
                "authorization_digest": self.snapshot.authorization_digest,
                "node_contract_digest": self.snapshot.node_contract_digest}}
        (run_dir(self.paths, self.snapshot.run_id) / "daemon-start.json").write_text(json.dumps(intent))
        self.assertFalse(SupervisorLease(self.paths, pid=child_pid).acquire(self.snapshot.run_id))

    def test_recovery_child_cannot_steal_from_a_live_original_daemon(self):
        import os
        import json
        from vibe_guide.state import run_dir
        from vibe_guide.supervisor import SupervisorLease
        child_pid = os.getpid() + 100000
        directory = run_dir(self.paths, self.snapshot.run_id)
        (directory / dm.DAEMON_FILE).write_text(json.dumps({"pid": os.getpid()}))
        (directory / "daemon-start.json").write_text(json.dumps({
            "run_id": self.snapshot.run_id, "plan_id": self.snapshot.plan_id,
            "status": "spawned", "pid": child_pid,
            "recovery_owner": {"epoch": self.epoch, "session_id": "original-supervisor",
                "authorization_digest": self.snapshot.authorization_digest,
                "node_contract_digest": self.snapshot.node_contract_digest}}))
        self.assertFalse(SupervisorLease(self.paths, pid=child_pid).acquire(self.snapshot.run_id))

    def test_recovery_child_rejects_stale_or_symlink_start_intent(self):
        import os
        from vibe_guide.state import run_dir
        from vibe_guide.supervisor import SupervisorLease
        directory = run_dir(self.paths, self.snapshot.run_id)
        child_pid = os.getpid() + 100000
        intent = {"run_id": self.snapshot.run_id, "plan_id": self.snapshot.plan_id,
            "status": "spawned", "pid": child_pid,
            "recovery_owner": {"epoch": self.epoch, "session_id": "original-supervisor",
                "authorization_digest": self.snapshot.authorization_digest,
                "node_contract_digest": self.snapshot.node_contract_digest}}
        path = directory / "daemon-start.json"
        changed = json.loads(json.dumps(intent))
        changed["recovery_owner"]["authorization_digest"] = "0" * 64
        path.write_text(json.dumps(changed))
        self.assertFalse(SupervisorLease(self.paths, pid=child_pid).acquire(self.snapshot.run_id))
        source = self.paths.root / "copied-intent.json"
        source.write_text(json.dumps(intent))
        path.unlink()
        path.symlink_to(source)
        self.assertFalse(SupervisorLease(self.paths, pid=child_pid).acquire(self.snapshot.run_id))

    def test_recovery_process_handoff_runs_two_real_supervisor_cycles(self):
        import subprocess
        import sys
        from pathlib import Path
        from vibe_guide.supervisor import recover_daemon
        from vibe_guide.state import run_dir
        command = ("import json,sys; from pathlib import Path; "
            "from vibe_guide.paths import ProjectPaths; from vibe_guide.models import Plan,DAGNode; "
            "from vibe_guide.monitor import Monitor; from vibe_guide.supervisor import Supervisor; "
            "from vibe_guide.daemon import Daemon,DaemonConfig; from vibe_guide.runners.fake import FakeRunner; "
            "import vibe_guide.protocol_adoption as adoption; "
            "adoption.effective_responsibility=lambda *a,**k: {'status':'effective'}; "
            "p=ProjectPaths(Path(sys.argv[1])); plan=Plan.from_dict(json.loads(sys.argv[3])); "
            "nodes=[DAGNode.from_dict(n) for n in json.loads(sys.argv[4])]; "
            "s=Supervisor(p,Monitor(p,plan,nodes),FakeRunner(),sys.argv[2]); "
            "d=Daemon(p,sys.argv[2],s,DaemonConfig(interval=0,heal_interval=1000000),"
            "assess=lambda:{'status':'ok','nodes':{}},notifier=lambda *a,**k:{'status':'sent'}); "
            "result=d.run(max_cycles=2); print(json.dumps(result)); "
            "sys.exit(0 if result.get('status')=='max_cycles' else 1)")
        processes = []
        def spawn(_product_command, **kwargs):
            child = subprocess.Popen([sys.executable, "-c", command, str(self.paths.root),
                self.snapshot.run_id, json.dumps(self.monitor.plan.to_dict()),
                json.dumps([node.to_dict() for node in self.monitor.nodes.values()])],
                **{**kwargs, "cwd": str(Path(dm.__file__).resolve().parent.parent)})
            processes.append(child)
            return child
        def starter(paths, run_id, plan_id, **kwargs):
            return dm.start_daemon(paths, run_id, plan_id, spawn=spawn, **kwargs)
        result = recover_daemon(self.paths, self.snapshot.run_id, owner_epoch=self.epoch,
            owner_session_id="original-supervisor", starter=starter)
        self.assertEqual(result["cycle_verification"], "pending_two_consecutive_cycles")
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].wait(timeout=15), 0,
            (run_dir(self.paths, self.snapshot.run_id) / dm.DAEMON_LOG_FILE).read_text())
        record = json.loads((run_dir(self.paths, self.snapshot.run_id) / dm.DAEMON_FILE).read_text())
        self.assertEqual(record["cycle_verification"]["status"], "verified")
        self.assertEqual(record["cycle_verification"]["owner_epoch"], self.epoch)
        self.assertEqual(record["pid"], processes[0].pid)

    def test_public_daemon_recovery_refuses_wrong_plan_before_spawn(self):
        from vibe_guide.cli import run_cli
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "original-supervisor"}, clear=True), patch("vibe_guide.daemon.start_daemon") as starter:
            result = run_cli(["daemon", "start", "--recover-daemon", "--run-id", self.snapshot.run_id,
                              "--plan", "wrong-plan", "--json"], self.paths.root)
        self.assertNotEqual(result.exit_code, 0)
        starter.assert_not_called()

    def test_actual_supervisor_two_cycles_are_bound_to_current_owner(self):
        from vibe_guide.supervisor import Supervisor
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.state import run_dir
        supervisor = Supervisor(self.paths, self.monitor,
                                FakeRunner(), self.snapshot.run_id, lease=self.lease)
        daemon = dm.Daemon(self.paths, self.snapshot.run_id, supervisor,
            dm.DaemonConfig(interval=0, heal_interval=0),
            assess=lambda: {"status": "ok", "nodes": {}}, notifier=lambda *a, **k: {"status": "sent"})
        daemon.tick()
        first = json.loads((run_dir(self.paths, self.snapshot.run_id) / dm.DAEMON_FILE).read_text())
        self.assertEqual(first["cycle_verification"]["status"], "pending")
        daemon.tick()
        second = json.loads((run_dir(self.paths, self.snapshot.run_id) / dm.DAEMON_FILE).read_text())
        self.assertEqual(second["cycle_verification"]["status"], "verified")
        self.assertEqual(second["cycle_verification"]["owner_epoch"], self.epoch)
        self.assertEqual(len(second["cycle_verification"]["cycles"]), 2)
        with patch.object(supervisor, "run_once", side_effect=OSError("query timeout")):
            daemon.tick()
        failed = json.loads((run_dir(self.paths, self.snapshot.run_id) / dm.DAEMON_FILE).read_text())
        self.assertNotIn("cycle_verification", failed)
        daemon.tick()
        recovered = json.loads((run_dir(self.paths, self.snapshot.run_id) / dm.DAEMON_FILE).read_text())
        self.assertEqual(recovered["cycle_verification"]["status"], "pending")


class HelpCausalityR7Tests(unittest.TestCase):
    def setUp(self):
        from tests import test_monitor as fixture
        from tests.test_r4_recovery_claims import NativeRecoveryTerminalTests
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.task_registry import TaskBinding, load_task_binding
        from vibe_guide.providers import CODEX_PROVIDER
        from vibe_guide.authorization import authorize, build_authorization_card
        from vibe_guide import protocol_adoption
        from vibe_guide.supervisor import SupervisorLease
        import os
        from vibe_guide.adapters.task_provider import ProviderActionStore
        class VisibleRunner(FakeRunner):
            def task_binding(self, contract, worktree, run_id, status):
                return TaskBinding(provider=CODEX_PROVIDER, mode='visible', topology='dual-visible',
                    task_id='original-worker', host='local', issue_id=contract['node_id'],
                    role=contract['role'], worktree=str(worktree), branch=contract['branch'],
                    run_id=run_id, status=status, generation=contract['generation'],
                    allowlist=list(contract.get('files', [])), cursor='original-cursor')
        self.fixture = fixture.MonitorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.paths = self.fixture.paths
        node = fixture.node('n1')
        node.contract['project_id'] = 'fixture-project'
        self.monitor, old = self.fixture.authorized_monitor([node])
        runner = VisibleRunner()
        with patch.object(self.monitor, '_context_allows_dispatch', return_value=False):
            self.snapshot = self.monitor.start(old, runner)
        approval = self.paths.root / 'approval.md'
        approval.write_text('explicit complete r4 adoption in isolated reviewer fixture')
        manifest = protocol_adoption.responsibility_manifest()
        receipt = protocol_adoption.record_adoption(self.paths, plan_id=self.snapshot.plan_id,
            run_id=self.snapshot.run_id, manifest=manifest, accepted_protocols=manifest['protocols'],
            evidence_ref='approval.md', evidence_sha256=hashlib.sha256(approval.read_bytes()).hexdigest(),
            authorization_digest=old.digest, contract_digest=old.node_contract_digest)
        card = build_authorization_card(self.monitor.plan, [node], self.fixture.capabilities,
            topology_rulings=self.monitor._topology_rulings,
            ruleset_digest=manifest['digest'], adoption_digest=receipt['adoption_digest'])
        self.snapshot = self.monitor.reauthorize(self.snapshot.run_id, authorize(card, 'AUTHORIZE'),
            runner, 'executable_contract_changed')
        self.assertEqual(protocol_adoption.effective_responsibility(self.paths, self.snapshot)['status'], 'effective')
        self.binding = load_task_binding(self.paths, 'n1', 'developer', run_id=self.snapshot.run_id)
        self.lease = SupervisorLease(self.paths)
        self.epoch = self.lease.takeover(self.snapshot.run_id, 0, owner={'session_id': 'supervisor-1', 'provider': 'codex'})
        self.store = ProviderActionStore(self.paths)
        self.home = self.paths.root / 'native-home'
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.home), 'CODEX_THREAD_ID': 'supervisor-1'})
        env.start()
        self.addCleanup(env.stop)
        directory = self.home / 'sessions/2026/11/09'
        directory.mkdir(parents=True)
        self.worker = directory / 'rollout-test-original-worker.jsonl'
        self.observer = directory / 'rollout-test-supervisor-1.jsonl'
        self.worker_rows = [{'type': 'session_meta', 'payload': {'id': self.binding.task_id}}]
        self.observer_rows = [{'type': 'session_meta', 'payload': {'id': 'supervisor-1'}}]
        self.step_a = {'command': 'python -m unittest command_A', 'cwd': self.binding.worktree}
        self.step_b = {'command': 'python -m unittest command_B', 'cwd': self.binding.worktree}
        self.step = self.step_a
        NativeRecoveryTerminalTests.add_turn(self, 'failure-A', 'run A', 1, 1)
        self.step = self.step_b
        NativeRecoveryTerminalTests.add_turn(self, 'failure-B', 'run B', 2, 3)
        self.step = self.step_a
        NativeRecoveryTerminalTests.flush(self)
        self.add_turn = lambda *a: NativeRecoveryTerminalTests.add_turn(self, *a)
        self.flush = lambda: NativeRecoveryTerminalTests.flush(self)
        self.finish = lambda action, code: NativeRecoveryTerminalTests.finish(self, action, code)
        from vibe_guide.state import run_dir
        self.help_file = run_dir(self.paths, self.snapshot.run_id) / 'heal-state.json'
        self.help_file.write_text(json.dumps({'nodes': {'n1': {'supervisor_responsibility': {
            'status': 'pending', 'node_id': 'n1', 'role': 'developer',
            'authorization_digest': self.snapshot.authorization_digest,
            'node_contract_digest': self.snapshot.node_contract_digest}}}}))
        for label in ('A', 'B'):
            (self.paths.root / ('diagnosis-' + label + '.md')).write_text('failure of command ' + label)

    def cli(self, *args):
        from vibe_guide.cli import run_cli
        result = run_cli(['supervisor-mailbox', '--run-id', self.snapshot.run_id, *args, '--json'], self.paths.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        return result.payload

    def help(self, label, *, bound=True):
        evidence = self.paths.root / ('diagnosis-' + label + '.md')
        body = dict(diagnosis='command ' + label + ' failed', action='repair command ' + label,
            next_action='retry command ' + label, verification_condition='command ' + label + ' exits zero',
            evidence_ref=evidence.name, evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())
        if bound:
            body['original_step'] = self.step_a if label == 'A' else self.step_b
        path = self.paths.root / 'help-input.json'
        path.write_text(json.dumps(body))
        result = self.cli('--node', 'n1', '--record-help', '--payload', str(path))
        self.selected_help = result['help_digest']
        return result

    def prepare(self):
        evidence = self.paths.root / 'recovery-diagnosis.md'
        evidence.write_text('independent diagnostic source for recovery A')
        path = self.paths.root / 'recovery-input.json'
        path.write_text(json.dumps(dict(prompt='repair and retry command A only, attempt ' + str(len(self.observer_rows)), original_step=self.step_a,
            help_digest=self.selected_help,
            evidence_ref=evidence.name, evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())))
        prepared = self.cli('--node', 'n1', '--role', 'developer', '--prepare-recovery', '--payload', str(path))
        self.assertEqual(prepared['status'], 'claimed')
        return self.store.action(prepared['action_id'])

    def exercise(self, help_label, exit_code=0, later_help=False):
        from vibe_guide.supervisor import pending_supervisor_responsibilities
        helped = self.help(help_label)
        action = self.prepare()
        if later_help:
            helped = self.help('B')
        self.finish(action, exit_code)
        result = self.cli('--reconcile-recovery', action['action_id'])
        self.assertEqual(result['outcome'], 'success' if exit_code == 0 else 'failed')
        item = json.loads(self.help_file.read_text())['nodes']['n1']['supervisor_responsibility']
        self.assertEqual(item['attempted_actions'][-1]['help_digest'], helped['help_digest'])
        pending = pending_supervisor_responsibilities(self.paths, self.snapshot.run_id)
        command_b = [row['payload']['item']['exitCode'] for row in self.worker_rows
                     if row.get('payload', {}).get('item', {}).get('command') == self.step_b['command']]
        print('HELP_CAUSALITY', json.dumps(dict(run_id=self.snapshot.run_id, help=help_label, later_help=later_help,
            step='A', outcome=result['outcome'], responsibility=item['status'],
            help_status=item['attempted_actions'][-1]['status'], help_digest=helped['help_digest'],
            pending_count=len(pending), command_B_exit_codes=command_b,
            real_adoption=True, mocked_effective=False, manual_claim_or_terminal=False)), flush=True)
        records = item['attempted_actions']
        if help_label == 'A':
            self.assertEqual(records[0]['status'], 'verified_effective' if exit_code == 0 else 'verified_ineffective')
        else:
            self.assertEqual(records[0]['status'], 'awaiting_original_step_verification')
        before = self.help_file.read_bytes()
        self.cli('--reconcile-recovery', action['action_id'])
        self.assertEqual(self.help_file.read_bytes(), before)
        if help_label == 'B' or later_help or exit_code != 0:
            self.assertEqual(item['status'], 'pending', 'A result must not verify unrelated B help')
            self.assertEqual(len(pending), 1)
        else:
            self.assertEqual(item['status'], 'verified')
            self.assertEqual(pending, [])

    def test_A_success_does_not_resolve_B_help(self):
        self.exercise('B')

    def test_matching_A_success_resolves_A_help(self):
        self.exercise('A')

    def test_matching_A_failure_remains_pending(self):
        self.exercise('A', exit_code=1)

    def test_later_B_help_is_not_resolved_by_A(self):
        self.exercise('A', later_help=True)


    def test_legacy_help_is_readable_but_never_guessed(self):
        from vibe_guide.supervisor import pending_supervisor_responsibilities
        self.help('A', bound=False)
        action = self.prepare()
        self.finish(action, 0)
        self.assertEqual(self.cli('--reconcile-recovery', action['action_id'])['outcome'], 'success')
        pending = pending_supervisor_responsibilities(self.paths, self.snapshot.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['attempted_actions'][0]['status'], 'awaiting_original_step_verification')

    def test_same_command_new_failure_does_not_rebind_old_help(self):
        self.help('A')
        self.add_turn('failure-A-again', 'run A again', 3, 5)
        self.flush()
        action = self.prepare()
        self.finish(action, 0)
        self.cli('--reconcile-recovery', action['action_id'])
        item = json.loads(self.help_file.read_text())['nodes']['n1']['supervisor_responsibility']
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['attempted_actions'][0]['status'], 'awaiting_original_step_verification')

    def test_bound_help_without_explicit_recovery_reference_stays_pending(self):
        self.help('A')
        self.selected_help = None
        action = self.prepare()
        self.finish(action, 0)
        self.cli('--reconcile-recovery', action['action_id'])
        item = json.loads(self.help_file.read_text())['nodes']['n1']['supervisor_responsibility']
        self.assertEqual(item['status'], 'pending')

    def test_unknown_original_step_rejected_without_help_write(self):
        from vibe_guide.supervisor import record_supervisor_help
        evidence = self.paths.root / 'diagnosis-A.md'
        body = dict(diagnosis='unknown failure', action='retry', next_action='verify',
            verification_condition='exit zero', evidence_ref=evidence.name,
            evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
            original_step=dict(command='never executed', cwd=self.binding.worktree))
        before = self.help_file.read_bytes()
        with self.assertRaises(PermissionError):
            record_supervisor_help(self.paths, self.snapshot.run_id, 'n1', body,
                owner_epoch=self.epoch, owner_session_id='supervisor-1')
        self.assertEqual(self.help_file.read_bytes(), before)

    def complete_public_recovery(self, action, code):
        observer_start, worker_start = len(self.observer_rows), len(self.worker_rows)
        self.finish(action, code)
        clock = 1000 * (action['sequence'] + 1)
        for row in self.observer_rows[observer_start:]:
            payload = row.get('payload', {})
            if 'started_at_ms' in payload:
                payload['started_at_ms'], payload['completed_at_ms'] = clock, clock + 1
        for row in self.worker_rows[worker_start:]:
            if 'timestamp' in row:
                row['timestamp'] = clock + 20
            payload = row.get('payload', {})
            if 'started_at_ms' in payload:
                payload['started_at_ms'], payload['completed_at_ms'] = clock + 20, clock + 21
        self.flush()
        result = self.cli('--reconcile-recovery', action['action_id'])
        self.assertEqual(result['outcome'], 'success' if code == 0 else 'failed')

    def assert_public_replays_preserve_current(self, first, second, expected_status, statuses):
        from vibe_guide.state import load_snapshot, load_events, run_dir
        item = json.loads(self.help_file.read_text())['nodes']['n1']['supervisor_responsibility']
        self.assertEqual(item['status'], expected_status)
        self.assertEqual([entry['status'] for entry in item['attempted_actions']], statuses)
        self.assertEqual(item['verification']['action_id'], second['action_id'])
        before = self.help_file.read_bytes()
        events = load_events(self.paths, self.snapshot.run_id)
        for restore in (False, True):
            if restore:
                directory = run_dir(self.paths, self.snapshot.run_id)
                (directory / 'state.json').write_text('corrupt isolated snapshot')
                self.assertEqual(load_snapshot(self.paths, self.snapshot.run_id).run_id, self.snapshot.run_id)
            for action in (second, first, first, second):
                self.cli('--reconcile-recovery', action['action_id'])
                self.assertEqual(self.help_file.read_bytes(), before)
                self.assertEqual(load_events(self.paths, self.snapshot.run_id), events)

    def test_public_old_failure_cannot_replace_new_success_after_replay(self):
        self.help('A')
        first = self.prepare()
        self.complete_public_recovery(first, 1)
        self.help('A')
        second = self.prepare()
        self.complete_public_recovery(second, 0)
        self.assert_public_replays_preserve_current(first, second, 'verified',
            ['verified_ineffective', 'verified_effective'])

    def test_public_old_success_cannot_replace_new_failure_with_B_pending(self):
        self.help('A')
        first = self.prepare()
        self.help('B')
        self.complete_public_recovery(first, 0)
        self.add_turn('A-new-failure', 'new A failure', 9, 1500)
        self.flush()
        self.help('A')
        second = self.prepare()
        self.complete_public_recovery(second, 1)
        self.assert_public_replays_preserve_current(first, second, 'pending',
            ['verified_effective', 'awaiting_original_step_verification', 'verified_ineffective'])

    def test_older_success_cannot_clear_a_later_failed_attempt(self):
        self.help('A')
        first = self.prepare()
        self.finish(first, 1)
        self.cli('--reconcile-recovery', first['action_id'])
        self.help('A')
        second = self.prepare()
        self.finish(second, 0)
        self.cli('--reconcile-recovery', second['action_id'])
        state = json.loads(self.help_file.read_text())
        item = state['nodes']['n1']['supervisor_responsibility']
        self.assertEqual(item['status'], 'verified')
        # Reorder existing authentic historical records: an old success is not
        # evidence that a later failed attempt was effective.
        item['attempted_actions'].reverse()
        item['status'] = 'pending'
        self.help_file.write_text(json.dumps(state))
        claims = next(self.store._directory('recovery-claims').glob('*.json'))
        attempt = json.loads(claims.read_text())['attempts'][0]
        self.store._project_recovery_help(first, attempt, 'failed')
        item = json.loads(self.help_file.read_text())['nodes']['n1']['supervisor_responsibility']
        self.assertEqual(item['status'], 'pending')



class SupervisorResponsibilityTests(Base):
    def setUp(self):
        super().setUp()
        rules = patch("vibe_guide.protocol_adoption.effective_responsibility",
                      return_value={"status": "effective"})
        rules.start()
        self.addCleanup(rules.stop)

    def test_two_cycles_do_not_start_parallel_llm_or_manufacture_recovery_attempts(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        snapshot = SimpleNamespace(nodes={NODE: {"status": "running"}}, tasks={})
        daemon.heal_pass(snapshot)
        daemon.heal_pass(snapshot)
        self.assertEqual(self.sessions, [])

        self.assertEqual(self.retries, [])
        record = daemon._heal_state()["nodes"][NODE]
        self.assertEqual(record["repairs"], 0)
        self.assertEqual(record["supervisor_responsibility"]["status"], "pending")
        self.assertEqual(self.pushed, [])

    def test_pending_responsibility_survives_process_and_is_actionable_on_next_beat(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        daemon.heal_pass(SimpleNamespace(nodes={NODE: {"status": "running"}}, tasks={}))
        fresh = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        fresh.heal_pass(SimpleNamespace(nodes={NODE: {"status": "running"}}, tasks={}))
        payload = {"state": "idle", "end_turn": True}
        text = _beat_extras(self.paths, RUN, payload, "idle")
        self.assertEqual(payload["state"], "work")
        self.assertFalse(payload["end_turn"])
        self.assertEqual(payload["supervisor_responsibilities"][0]["node_id"], NODE)
        self.assertIn("复核", text)
        self.assertEqual(self.sessions, [])

    def test_repeated_notification_suppression_does_not_settle_the_problem(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        snapshot = SimpleNamespace(nodes={NODE: {"status": "running"}}, tasks={})
        for _ in range(5):
            daemon.heal_pass(snapshot)
        current = daemon._heal_state()["nodes"][NODE]
        self.assertFalse(current["escalated"])
        self.assertEqual(current["supervisor_responsibility"]["status"], "pending")
        self.assertEqual(self.sessions, [])

    def test_next_cycle_preserves_original_supervisor_diagnosis_action_and_verification(self):
        daemon = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        snapshot = SimpleNamespace(nodes={NODE: {"status": "running"}}, tasks={})
        daemon.heal_pass(snapshot)
        saved = daemon._heal_state()
        pending = saved["nodes"][NODE]["supervisor_responsibility"]
        details = {"diagnosis": {"evidence_ref": "failure.md", "finding": "wrong invocation"},
                   "attempted_actions": [{"action": "correct syntax", "result": "not resolved"}],
                   "responsible_task": "original-worker", "next_action": "check supported option",
                   "verification_condition": "original failed command passes"}
        pending.update(details)
        daemon._save_heal_state(saved)
        fresh = self.daemon(FakeSupervisor(["running"]), assess=lambda: stalled())
        fresh.heal_pass(snapshot)
        restored = fresh._heal_state()["nodes"][NODE]["supervisor_responsibility"]
        for key, value in details.items():
            self.assertEqual(restored.get(key), value)
        self.assertEqual(self.sessions, [])
