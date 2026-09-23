import unittest

from vibe_guide.adapters.base import Environment
from vibe_guide.adapters.registry import AdapterRegistry


class NativeDispatchTests(unittest.TestCase):
    def test_non_codex_never_falls_back_to_background(self):
        env = Environment(commands={"claude-code.agent": True}, facts={
            "claude-code.shell": True, "claude-code.subprocess": True,
            "claude-code.worktree": True,
        })
        adapter = AdapterRegistry(background_launchers={"claude-code": lambda: {}}).get("claude-code")
        caps = adapter.detect(env).capabilities
        self.assertEqual(caps.mode, "guide")

    def test_agent_presence_alone_cannot_verify_codex_native_dispatch(self):
        env = Environment(commands={"codex.agent": True}, facts={
            "codex.shell": True, "codex.subprocess": True, "codex.worktree": True,
        })
        caps = AdapterRegistry().get("codex").detect(env).capabilities
        self.assertEqual(caps.mode, "guide")

    def test_codex_native_missing_cannot_fall_back_to_background(self):
        env = Environment(commands={"codex.agent": True}, facts={
            "codex.shell": True, "codex.subprocess": True, "codex.worktree": True,
        })
        adapter = AdapterRegistry(background_launchers={"codex": lambda: {}}).get("codex")
        self.assertEqual(adapter.detect(env).capabilities.mode, "guide")

    def test_claude_code_visible_after_observed_native_lifecycle_facts(self):
        """Observed lifecycle facts, not the adapter id, decide visibility."""
        env = Environment(commands={"claude-code.agent": True}, facts={
            "claude-code.shell": True, "claude-code.subprocess": True,
            "claude-code.worktree": True, "claude-code.visible_task.create": True,
            "claude-code.visible_task.enter": True,
            "claude-code.visible_task.resume": True,
            "claude-code.visible_task.wait": True,
        })
        caps = AdapterRegistry().get("claude-code").detect(env).capabilities
        self.assertEqual(caps.mode, "visible")
        self.assertEqual(caps.provider, "claude-code-visible")
        self.assertTrue(caps.create_task and caps.enter_task and caps.resume_task and caps.wait_task)

    def test_claude_code_manifest_declares_native_control_plane(self):
        """The manifest, not an adapter id comparison, carries the declaration."""
        manifest = AdapterRegistry().get("claude-code").manifest
        self.assertIs(manifest["native_control_plane"], True)


class DispatchTopologyRulingWiringTests(unittest.TestCase):
    """ISSUE-04: the CLI hands the observed platform ruling to the monitor."""

    def test_observed_topology_rulings_fail_closed_without_capability_bridge(self):
        import tempfile
        from pathlib import Path

        from vibe_guide.cli import _observed_topology_rulings
        from vibe_guide.paths import ProjectPaths

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_observed_topology_rulings(ProjectPaths(Path(tmp))), {})

    def test_observed_topology_rulings_reads_attested_in_session_sdd_fact(self):
        import tempfile
        from pathlib import Path

        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.cli import _observed_topology_rulings
        from vibe_guide.paths import ProjectPaths

        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            ProviderActionStore(paths).publish_capabilities(
                "codex",
                {"codex.agent": True, "codex.in_session_sdd": True},
                "test-provenance",
            )
            self.assertEqual(
                _observed_topology_rulings(paths), {"codex": "in_session_sdd"}
            )

    def test_observed_topology_rulings_without_probe_fact_stays_dual_visible(self):
        import tempfile
        from pathlib import Path

        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.cli import _observed_topology_rulings
        from vibe_guide.paths import ProjectPaths

        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            ProviderActionStore(paths).publish_capabilities(
                "workbuddy", {"workbuddy.agent": True}, "test-provenance"
            )
            self.assertEqual(
                _observed_topology_rulings(paths), {"workbuddy": "dual-visible"}
            )

    def test_rev3_policy_card_fallback_uses_project_config_worker_session_limit(self):
        import json
        import tempfile
        from pathlib import Path

        from vibe_guide.cli import _load_plan
        from vibe_guide.paths import ProjectPaths

        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            directory = paths.vibe / "plans" / "vibe-guide-v3.9-bugfix"
            directory.mkdir(parents=True)
            (paths.vibe / "config.json").write_text(
                json.dumps({"max_active_worker_sessions": 2}), encoding="utf-8"
            )
            (directory / "plan.json").write_text(
                json.dumps(
                    {
                        "plan_id": "vibe-guide-v3.9-bugfix",
                        "version": 3,
                        "prd_path": "docs/prd.md",
                        "node_ids": ["n1"],
                        "status": "authorized",
                    }
                ),
                encoding="utf-8",
            )
            (directory / "nodes.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "n1",
                            "title": "n1",
                            "depends_on": [],
                            "integration_after": [],
                            "parallel_group": "g1",
                            "contract": {
                                "files": ["n1.py"],
                                "worker": "codex",
                                "worktree": ".worktrees/n1",
                                "branch": "branch-n1",
                            },
                            "status": "planned",
                            "worktree": ".worktrees/n1",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            # The Rev3 policy projection is not an executable card; the
            # loader derives the envelope and must take the session limit
            # from the project config instead of a hardcoded constant.
            (directory / "authorization-card.json").write_text(
                json.dumps({"worker_policy": {"mode": "rev3"}}), encoding="utf-8"
            )

            _, _, _, card = _load_plan(paths, "vibe-guide-v3.9-bugfix")

            self.assertEqual(card.active_pair_limit, 2)


if __name__ == "__main__":
    unittest.main()
