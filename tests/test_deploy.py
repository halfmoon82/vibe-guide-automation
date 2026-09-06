import unittest

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.deploy import (
    DeployManifest,
    DeployState,
    authorize_deploy,
    plan_deploy,
    verify_deploy,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


class DeployTests(unittest.TestCase):
    def manifest(self):
        return DeployManifest(
            target="staging",
            commit="a" * 40,
            command_allowlist=["./scripts/deploy-staging"],
            health_checks=[{"name": "http", "kind": "http", "required": True}],
            rollback={"commit": "b" * 40, "command": "./scripts/rollback-staging"},
            stop_conditions=["health_check_failed", "version_mismatch"],
        )

    def test_independent_acceptance_is_required_and_manifest_is_not_implicit(self):
        manifest = self.manifest()
        self.assertEqual(plan_deploy(manifest, "review").status, "blocked_deploy")
        self.assertEqual(plan_deploy(manifest, "accepted").status, "deploy_planned")

    def test_deploy_authorization_is_separate_and_exact(self):
        manifest = self.manifest()
        with self.assertRaises(PermissionError):
            authorize_deploy(manifest, "AUTHORIZE")
        record = authorize_deploy(manifest, "AUTHORIZE DEPLOY")
        self.assertEqual(record.manifest_digest, manifest.digest)
        self.assertIn("deploy", record.allowed_actions)

    def test_normal_plan_authorization_cannot_start_deploy(self):
        manifest = self.manifest()
        plan = Plan("p1", 1, "prd.md", ["n1"], "draft")
        node = DAGNode("n1", "n1", [], [], "g", {"files": ["safe.py"]}, "ready")
        caps = AgentCapabilities("codex", True, True, True, True, True, "full")
        ordinary = authorize(build_authorization_card(plan, [node], caps), "AUTHORIZE")
        with self.assertRaises(PermissionError):
            from vibe_guide.deploy import start_deploy
            start_deploy(manifest, plan_deploy(manifest, "accepted"), ordinary)

    def test_health_and_version_must_be_observable(self):
        manifest = self.manifest()
        unknown = verify_deploy(manifest, {"health": None, "version": None})
        self.assertEqual(unknown.status, "blocked_unknown")
        failed = verify_deploy(manifest, {"health": False, "version": manifest.commit})
        self.assertEqual(failed.status, "blocked_deploy")

    def test_success_and_rollback_are_explicit(self):
        manifest = self.manifest()
        deployed = verify_deploy(
            manifest, {"health": True, "version": manifest.commit}
        )
        self.assertEqual(deployed.status, "deployed")
        rolled_back = verify_deploy(
            manifest,
            {
                "health": False,
                "version": manifest.commit,
                "rollback": {"health": True, "version": manifest.rollback["commit"]},
            },
        )
        self.assertEqual(rolled_back.status, "rolled_back")

    def test_manifest_rejects_unbounded_or_unsafe_commands(self):
        with self.assertRaises(ValueError):
            DeployManifest(
                "staging", "a" * 40, ["./deploy; rm -rf /"], [], {"commit": "b" * 40}
            )


if __name__ == "__main__":
    unittest.main()
