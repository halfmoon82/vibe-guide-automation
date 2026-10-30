import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide import daemon as dm
from vibe_guide import stall
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import run_dir

RUN = "run-" + "c" * 32
NODE = "slugify"


def parked_node(generation):
    """A node as the sandbox run left it: dispatch fails with binding_unknown every cycle."""
    return {
        "status": "blocked_unknown",
        "user_status": "自动修复中",
        "binding_phase": "binding_unknown",
        "developer_generation": generation,
        "review_generation": 0,
        "active_task": None,
        "start_intent": None,
        "evidence": [],
        "handoff_file": "",
        "status_file": "",
        "retryable_action": {
            "attempt": 1, "reason_class": "binding_unknown", "role": "developer",
            "next_retry_at": 1793245300.0 + generation, "last_observation_ref": "provider-pending",
        },
    }


class DispatchLoopFingerprintTests(unittest.TestCase):
    def test_redispatch_generation_bump_is_not_progress(self):
        self.assertEqual(stall.node_fingerprint(parked_node(1)), stall.node_fingerprint(parked_node(64)))

    def test_generation_still_counts_when_a_session_is_bound(self):
        running = parked_node(1)
        running.update(status="running", active_task={"role": "developer", "task_id": "t", "generation": 1})
        running["retryable_action"] = None
        moved = copy.deepcopy(running)
        moved["developer_generation"] = 2
        self.assertNotEqual(stall.node_fingerprint(running), stall.node_fingerprint(moved))

    def test_only_binding_unknown_is_treated_as_a_dispatch_loop(self):
        retry = parked_node(1)
        retry["retryable_action"]["reason_class"] = "retryable"
        moved = copy.deepcopy(retry)
        moved["developer_generation"] = 2
        self.assertNotEqual(stall.node_fingerprint(retry), stall.node_fingerprint(moved))

    def test_three_failed_dispatch_cycles_read_as_stalled(self):
        previous = None
        for generation in (1, 2, 3):
            outcome = stall.assess_stalls({NODE: parked_node(generation)}, {}, [], previous)
            previous = outcome["record"]
        result = outcome["nodes"][NODE]
        self.assertEqual(result["unchanged_beats"], 3)
        self.assertEqual(result["tier"], stall.TIER_INVESTIGATE)


class DaemonLadderOnDispatchLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name), vibe_home=Path(self.tmp.name) / "home")
        (Path(self.tmp.name) / "home").mkdir()
        run_dir(self.paths, RUN, create=True)
        self.generation = 0
        self.pushed = []
        self.sessions = []

    def snapshot(self):
        self.generation += 1
        return SimpleNamespace(status="blocked_unknown", tasks={}, authorization={},
                               nodes={NODE: parked_node(self.generation)})

    def test_node_climbs_retry_then_short_session_then_is_recorded_while_generation_keeps_growing(self):
        from vibe_guide import short_session as ss

        def launcher(platform, prompt, **kwargs):
            self.sessions.append(prompt)
            return ss.ShortSessionResult(platform, "ok", text="VIBE_HEAL: not_fixed")

        entry = {"tier": stall.TIER_INVESTIGATE, "pack": {"role": "developer", "kind": "dispatch"}}
        retries = []
        daemon = dm.Daemon(
            self.paths, RUN, SimpleNamespace(), dm.DaemonConfig(interval=0, heal_interval=0),
            sleep=lambda s: None, launcher=launcher,
            notifier=lambda m, k, n="": self.pushed.append(k) or {"status": "sent"},
            assess=lambda: {"status": "ok", "nodes": {NODE: entry}},
            verify=lambda n, r: {"status": "verify_failed"}, retry=retries.append)
        for _ in range(5):
            daemon.heal_pass(self.snapshot())
        self.assertEqual(retries, [NODE])
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(self.pushed, [])
        self.assertIn(NODE, daemon._heal_state()["unresolved"])


if __name__ == "__main__":
    unittest.main()
