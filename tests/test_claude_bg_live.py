"""Real-chain acceptance for Claude Code background dispatch (``claude --bg``).

Skipped unless VIBE_LIVE_CLAUDE_BG=1: it starts real background sessions with
the user-level ``claude_bg`` config, spends API tokens and takes minutes.

Chain: dispatch one developer node -> worker delivery -> independent review
finds one defect -> rework resumes the SAME session -> re-review passes.
Every step is printed so the run log is the acceptance record; any failure or
timeout fails the test with the original reason (no test double stands in).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LIVE = os.environ.get("VIBE_LIVE_CLAUDE_BG") == "1"
POLL_SECONDS = 10
DELIVERY_TIMEOUT = float(os.environ.get("VIBE_LIVE_TIMEOUT", "600"))
RUN_ID = "run-live"
NODE = "greet"

CONTRACT = """你是 developer，Issue {node}。只在当前目录工作，不要提交 git。
1. 新建 greet.py，定义函数 greet(name)，返回 "Hello, " + name。
2. 完成后写交付文件 {delivery}（相对当前目录），内容恰好是 JSON：
   {{"payload": {{"node": "{node}", "generation": 1, "files": ["greet.py"]}}}}
写完交付文件就结束，不要做别的。"""

REWORK = """独立审查发现 1 个缺陷：greet("") 返回 "Hello, "，应返回 "Hello, stranger"。
请修改 greet.py 修复它（其余行为不变），然后写交付文件 {delivery}，内容恰好是 JSON：
{{"payload": {{"node": "{node}", "generation": 2, "files": ["greet.py"]}}}}
写完交付文件就结束，不要做别的。"""


def log(step, **facts):
    print("[live] %s %s" % (step, json.dumps(facts, ensure_ascii=False)), file=sys.stderr, flush=True)


def review(project):
    """Independent check of greet.py; returns the list of findings."""
    probe = (
        "import greet\n"
        "out = {'named': greet.greet('Ann'), 'empty': greet.greet('')}\n"
        "import json; print(json.dumps(out))"
    )
    done = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.')\n" + probe],
                          cwd=project, capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        return ["greet.py does not run: " + done.stderr.strip()[-200:]]
    out = json.loads(done.stdout)
    findings = []
    if out["named"] != "Hello, Ann":
        findings.append("greet('Ann') returned %r" % out["named"])
    if out["empty"] != "Hello, stranger":
        findings.append("greet('') returned %r, expected 'Hello, stranger'" % out["empty"])
    return findings


@unittest.skipUnless(LIVE, "set VIBE_LIVE_CLAUDE_BG=1 to run the real claude --bg chain")
class LiveBackgroundChainTests(unittest.TestCase):
    def setUp(self):
        from vibe_guide.adapters.claude_bg import ClaudeBgLauncher, load_claude_bg_config
        from vibe_guide.adapters.task_provider import BackgroundTaskRouting
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.runners.claude_bg import ClaudeBgRunner

        self.tmp = tempfile.TemporaryDirectory(prefix="vibe-live-")
        self.project = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=self.project, check=True)
        self.paths = ProjectPaths(self.project)
        # Dispatch into a linked worktree like real vibe nodes: newer Claude
        # Code background sessions refuse writes in the main checkout.
        self.worktree = self.project / ".worktrees" / NODE
        subprocess.run(["git", "worktree", "add", "-q", str(self.worktree), "-b", "node/" + NODE],
                       cwd=self.project, check=True)
        (self.worktree / ".vibe" / "provider-actions" / "deliveries").mkdir(parents=True)
        config = load_claude_bg_config(self.paths)
        routing = BackgroundTaskRouting(host="local", worktree=str(self.worktree), branch="node/" + NODE,
                                        status_file="status.json", handoff_file="handoff.json")
        self.launcher = ClaudeBgLauncher(routing, config, model=config.model)
        # Deliveries are written relative to the worker's cwd (the worktree).
        self.runner = ClaudeBgRunner(lambda contract, worktree: self.launcher,
                                     paths=ProjectPaths(self.worktree))
        self.task_id = None
        log("setup", project=str(self.project), model=config.model)

    def tearDown(self):
        if self.task_id:
            if os.environ.get("VIBE_LIVE_KEEP_LOGS") == "1":
                logs = subprocess.run(["claude", "logs", self.task_id[:8]], cwd=self.worktree,
                                      capture_output=True, text=True, timeout=30)
                tail = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", " ", logs.stdout)[-1500:]
                log("session_logs", tail=tail)
            for op in (self.launcher.stop, self.launcher.remove):
                try:
                    op(self.task_id)
                except Exception as error:  # cleanup is best effort, but say so
                    log("cleanup_failed", op=op.__name__, reason=str(error))
            log("cleanup", task_id=self.task_id)
        self.tmp.cleanup()

    def sessions_here(self):
        # Only this test's worktree: the machine-wide list includes the user's
        # own interactive sessions, which change independently.
        return sorted(e["sessionId"] for e in self.launcher._entries()
                      if self.launcher._same_dir(e.get("cwd")))

    def delivery(self, generation):
        return ".vibe/provider-actions/deliveries/{}-{}-developer-g{}.json".format(RUN_ID, NODE, generation)

    def wait_delivered(self, handle):
        deadline = time.time() + DELIVERY_TIMEOUT
        states = []
        while time.time() < deadline:
            events = self.runner.poll(handle)
            state = self.launcher.status(self.task_id)
            if not states or states[-1] != state:
                states.append(state)
                log("state", task_id=self.task_id, state=state)
            for event in events:
                if event.event == "delivered":
                    return event, states
                self.fail("background chain stopped: %s %s" % (event.event, event.data.get("reason")))
            time.sleep(POLL_SECONDS)
        self.fail("no delivery within %ss; states seen: %s" % (DELIVERY_TIMEOUT, states))

    def test_dispatch_review_rework_same_session(self):
        contract_path = self.worktree / "contract.md"
        contract_path.write_text(CONTRACT.format(node=NODE, delivery=self.delivery(1)), encoding="utf-8")
        started = time.time()
        handle = self.runner.start({"node_id": NODE, "role": "developer", "generation": 1,
                                    "run_id": RUN_ID, "contract_path": str(contract_path)}, self.worktree)
        self.task_id = self.runner._sessions[handle.run_id]["claims"]["task_id"]
        log("dispatched", task_id=self.task_id, handle=handle.run_id)

        first, states = self.wait_delivered(handle)
        log("delivered", generation=1, payload=first.data["payload"], states=states,
            seconds=round(time.time() - started))
        findings = review(self.worktree)
        log("review", generation=1, findings=findings)
        self.assertEqual(len(findings), 1, "first review must find exactly the seeded defect")

        before = self.sessions_here()
        self.runner.resume(handle, REWORK.format(node=NODE, delivery=self.delivery(2)), generation=2)
        log("rework_resumed", task_id=self.task_id)
        second, states = self.wait_delivered(handle)
        after = self.sessions_here()
        log("delivered", generation=2, payload=second.data["payload"], states=states,
            seconds=round(time.time() - started))

        self.assertEqual(second.data["task_id"], first.data["task_id"])
        self.assertEqual(before, [self.task_id])
        self.assertEqual(after, [self.task_id], "rework must not create a copy session")
        findings = review(self.worktree)
        log("review", generation=2, findings=findings)
        self.assertEqual(findings, [])
        log("accepted", task_id=self.task_id, same_session=True, seconds=round(time.time() - started))


if __name__ == "__main__":
    unittest.main()
