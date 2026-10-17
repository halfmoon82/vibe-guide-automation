import json
from pathlib import Path
import tempfile
import unittest

from vibe_guide.adapters.claude_bg import ClaudeBgUnknown
from vibe_guide.contracts import RunHandle
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.claude_bg import ClaudeBgRunner

UUID = "1a2b3c4d-1111-4222-8333-444455556666"


class FakeLauncher:
    def __init__(self, state="working"):
        self.state = state
        self.calls = []

    def launch(self, role, issue_id, contract_path):
        self.calls.append(("launch", role, issue_id, str(contract_path)))
        return {"task_id": UUID}

    def status(self, task_id):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    def stop(self, task_id):
        self.calls.append(("stop", task_id))

    def resume(self, task_id, message):
        self.calls.append(("resume", task_id, message))


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.launcher = FakeLauncher()
        self.report = None
        self.runner = ClaudeBgRunner(lambda c, w: self.launcher, delivery_reader=lambda claims: self.report)
        self.contract = {"node_id": "n1", "role": "developer", "run_id": "r1", "generation": 1, "contract_path": "/c.md"}

    def start(self, **extra):
        return self.runner.start(dict(self.contract, **extra), Path("/tmp"))

    def test_start_launches_and_claims(self):
        handle = self.start()
        self.assertEqual(self.launcher.calls[0][:3], ("launch", "developer", "n1"))
        self.launcher.state = "blocked"
        data = self.runner.poll(handle)[0].data
        self.assertEqual((data["task_id"], data["node_id"], data["handle_id"], data["generation"]), (UUID, "n1", handle.run_id, 1))

    def test_working_is_quiet(self):
        self.assertEqual(self.runner.poll(self.start()), [])

    def test_done_without_delivery_is_not_complete(self):
        self.launcher.state = "done"
        events = self.runner.poll(self.start())
        self.assertEqual([e.event for e in events], ["blocked_unknown"])
        self.assertIn("without worker delivery", events[0].data["reason"])

    def test_delivery_is_delivered_once(self):
        handle = self.start()
        self.report = {"payload": {"event": "delivered", "summary": "ok"}}
        events = self.runner.poll(handle)
        self.assertEqual([e.event for e in events], ["delivered"])
        self.assertEqual(events[0].data["payload"]["summary"], "ok")
        self.assertEqual(self.runner.poll(handle), [])

    def test_state_mapping(self):
        for state, event in (("blocked", "needs_attention"), ("failed", "failed_retryable"), ("stopped", "stopped")):
            self.launcher.state = state
            self.assertEqual([e.event for e in self.runner.poll(self.start())], [event])

    def test_unknown_is_blocked_unknown_with_reason(self):
        self.launcher.state = ClaudeBgUnknown("claude command not found")
        events = self.runner.poll(self.start())
        self.assertEqual(events[0].event, "blocked_unknown")
        self.assertEqual(events[0].data["reason"], "claude command not found")

    def test_resume_task_id_continues_same_session(self):
        handle = self.start(resume_task_id=UUID, rework_message="fix P1")
        self.assertEqual(self.launcher.calls, [("resume", UUID, "fix P1")])
        self.assertEqual(self.launcher.calls.count(("stop", UUID)), 0)
        self.launcher.state = "blocked"
        self.assertEqual(self.runner.poll(handle)[0].data["task_id"], UUID)

    def test_stop_and_resume_on_handle(self):
        handle = self.start()
        self.runner.stop(handle)
        self.runner.resume(handle, "again", 2)
        self.assertEqual(self.launcher.calls[1:], [("stop", UUID), ("resume", UUID, "again")])

    def test_rework_ignores_old_generation_delivery(self):
        reports = {}
        runner = ClaudeBgRunner(
            lambda c, w: self.launcher,
            delivery_reader=lambda claims: reports.get(claims["generation"]),
        )
        handle = runner.start(dict(self.contract), Path("/tmp"))
        reports[1] = {"payload": {"summary": "g1"}}
        self.assertEqual([e.event for e in runner.poll(handle)], ["delivered"])
        runner.resume(handle, "rework", 2)
        self.launcher.state = "working"
        self.assertEqual(runner.poll(handle), [])
        reports[2] = {"payload": {"summary": "g2"}}
        events = runner.poll(handle)
        self.assertEqual(events[0].event, "delivered")
        self.assertEqual(events[0].data["generation"], 2)
        self.assertEqual(events[0].data["payload"]["summary"], "g2")

    def test_resume_generation_must_increase_and_be_int(self):
        handle = self.start()
        for bad in (1, 0, True, "2", None, 2.0):
            with self.assertRaises(ValueError):
                self.runner.resume(handle, "x", bad)
        self.assertEqual(self.launcher.calls, [("launch", "developer", "n1", "/c.md")])

    def test_start_resume_requires_generation(self):
        contract = dict(self.contract, resume_task_id=UUID, rework_message="m")
        del contract["generation"]
        with self.assertRaises(ValueError):
            self.runner.start(contract, Path("/tmp"))
        contract["generation"] = True
        with self.assertRaises(ValueError):
            self.runner.start(contract, Path("/tmp"))
        self.assertEqual(self.launcher.calls, [])

    def test_unknown_handle(self):
        ghost = RunHandle("claude-bg-n1-9")
        events = self.runner.poll(ghost)
        self.assertEqual(events[0].event, "blocked_unknown")
        self.assertEqual(events[0].data["handle_id"], "claude-bg-n1-9")
        self.assertIn("adopt", events[0].data["reason"])
        with self.assertRaises(ClaudeBgUnknown):
            self.runner.stop(ghost)
        with self.assertRaises(ClaudeBgUnknown):
            self.runner.resume(ghost, "x", 2)

    def test_adopt_rebinds_without_launching(self):
        handle = self.runner.adopt(dict(self.contract), Path("/tmp"), UUID)
        self.assertEqual(self.launcher.calls, [])
        self.launcher.state = "blocked"
        data = self.runner.poll(handle)[0].data
        self.assertEqual((data["task_id"], data["node_id"]), (UUID, "n1"))
        self.runner.stop(handle)
        self.assertEqual(self.launcher.calls, [("stop", UUID)])

    def test_adopt_rejects_bad_uuid(self):
        for bad in ("1a2b3c4d", UUID + "\n", "", None):
            with self.assertRaises(ValueError):
                self.runner.adopt(dict(self.contract), Path("/tmp"), bad)
        self.assertEqual(self.launcher.calls, [])

    def test_default_reader_reads_archived_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".vibe" / "provider-actions" / "deliveries").mkdir(parents=True)
            paths = ProjectPaths(root, vibe_home=root / "home")
            (root / "home").mkdir()
            runner = ClaudeBgRunner(lambda c, w: self.launcher, paths=paths)
            handle = runner.start(dict(self.contract), root)
            self.assertEqual(runner.poll(handle), [])
            target = root / ".vibe/provider-actions/deliveries/r1-n1-developer-g1.json"
            target.write_text(json.dumps({"payload": {"summary": "s"}}), encoding="utf-8")
            self.assertEqual(runner.poll(handle)[0].event, "delivered")

    def test_default_reader_rejects_symlink_inside_deliveries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            d = root / ".vibe" / "provider-actions" / "deliveries"
            d.mkdir(parents=True)
            (root / "home").mkdir()
            (d / "real.json").write_text(json.dumps({"payload": {"a": 1}}), encoding="utf-8")
            (d / "r1-n1-developer-g1.json").symlink_to(d / "real.json")
            runner = ClaudeBgRunner(lambda c, w: self.launcher, paths=ProjectPaths(root, vibe_home=root / "home"))
            handle = runner.start(dict(self.contract), root)
            self.assertEqual(runner.poll(handle), [])

    def test_default_reader_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            d = root / ".vibe" / "provider-actions" / "deliveries"
            d.mkdir(parents=True)
            (root / "home").mkdir()
            real = root / "real.json"
            real.write_text(json.dumps({"payload": {"a": 1}}), encoding="utf-8")
            (d / "r1-n1-developer-g1.json").symlink_to(real)
            runner = ClaudeBgRunner(lambda c, w: self.launcher, paths=ProjectPaths(root, vibe_home=root / "home"))
            handle = runner.start(dict(self.contract), root)
            self.assertEqual(runner.poll(handle), [])

    def test_monitor_payload_without_contract_path_launches_with_written_prompt(self):
        # The monitor's start payload carries spec_path/worktree/files, never
        # contract_path; a KeyError here was retried forever without launching.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".vibe").mkdir()
            (root / "home").mkdir()
            runner = ClaudeBgRunner(lambda c, w: self.launcher, paths=ProjectPaths(root, vibe_home=root / "home"))
            contract = {
                "node_id": "n1", "role": "developer", "run_id": "r1", "generation": 3,
                "spec_path": ".vibe/plans/p1/prd.md", "worktree": ".worktrees/n1",
                "branch": "node/n1", "files": ["a.py"], "output": "交付 X",
            }
            runner.start(contract, root / ".worktrees" / "n1")
            launch = [c for c in self.launcher.calls if c[0] == "launch"]
            self.assertEqual(len(launch), 1)
            prompt_path = Path(launch[0][3])
            self.assertTrue(prompt_path.is_file())
            self.assertTrue(str(prompt_path).startswith(str(root / ".vibe")))
            text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("vibe worker-deliver --run-id r1 --node n1 --role developer --generation 3", text)
            self.assertIn(".worktrees/n1", text)
            self.assertIn(".vibe/plans/p1/prd.md", text)


if __name__ == "__main__":
    unittest.main()
