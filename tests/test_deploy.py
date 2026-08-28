import unittest

from vibe_guide.authorization import authorize_deploy
from vibe_guide.deploy import (
    DeployManifest,
    deploy_manifest_digest,
    plan_deploy,
    prepare_deploy,
    start_deploy,
    verify_deploy,
)


def manifest(**overrides):
    data = {
        "target": "staging",
        "commit": "a" * 40,
        "command_allowlist": ["release", "restart"],
        "health_checks": [{"name": "http", "url": "https://staging/health"}],
        "rollback": {"version": "previous", "command": "rollback"},
        "stop_conditions": ["health check fails"],
    }
    data.update(overrides)
    return DeployManifest(**data)


class DeployTests(unittest.TestCase):
    def test_accepted_independent_state_plans_manifest_and_missing_fields_fail_closed(self):
        planned = plan_deploy(manifest(), "accepted")
        self.assertEqual(planned.status, "deploy_planned")
        self.assertEqual(planned.manifest.target, "staging")
        with self.assertRaises(ValueError):
            plan_deploy(DeployManifest("", "", [], [], {}), "accepted")
        with self.assertRaises(ValueError):
            plan_deploy(manifest(), "running")

    def test_deploy_authorization_is_separate_and_manifest_bound(self):
        item = manifest()
        record = authorize_deploy(item, "AUTHORIZE_DEPLOY")
        self.assertEqual(record.manifest_digest, deploy_manifest_digest(item))
        with self.assertRaises(ValueError):
            authorize_deploy(item, "AUTHORIZE")

    def test_unknown_health_or_version_is_blocked_unknown_and_failed_health_blocks_deploy(self):
        item = manifest()
        self.assertEqual(
            verify_deploy(item, {"version": None, "health": True}).status,
            "blocked_unknown",
        )
        self.assertEqual(
            verify_deploy(item, {"version": item.commit, "health": False}).status,
            "blocked_deploy",
        )
        self.assertEqual(
            verify_deploy(item, {"version": item.commit, "health": True}).status,
            "deployed",
        )

    def test_failed_deploy_with_verified_rollback_is_rolled_back(self):
        item = manifest()
        result = verify_deploy(
            item,
            {
                "version": item.commit,
                "health": False,
                "rollback": {"version": item.rollback["version"], "health": True},
            },
        )
        self.assertEqual(result.status, "rolled_back")

    def test_rollback_is_blocked_when_failed_deployment_version_mismatches_manifest(self):
        item = manifest()
        result = verify_deploy(
            item,
            {
                "version": "b" * 40,
                "health": False,
                "rollback": {"version": item.rollback["version"], "health": True},
            },
        )
        self.assertEqual(result.status, "blocked_deploy")
        self.assertIn("version", result.reason)

    def test_deploy_ready_and_running_are_explicit_authorized_transitions(self):
        item = manifest()
        planned = plan_deploy(item, "accepted")
        record = authorize_deploy(item, "AUTHORIZE_DEPLOY")
        ready = prepare_deploy(item, planned, record)
        self.assertEqual(ready.status, "deploy_ready")
        running = start_deploy(item, ready, record)
        self.assertEqual(running.status, "deploy_running")
        self.assertEqual(running.evidence, ready.evidence)

    def test_deploy_ready_and_running_reject_invalid_or_wrong_state(self):
        item = manifest()
        planned = plan_deploy(item, "accepted")
        record = authorize_deploy(item, "AUTHORIZE_DEPLOY")
        with self.assertRaises(PermissionError):
            prepare_deploy(item, planned, authorize_deploy(manifest(commit="c" * 40), "AUTHORIZE_DEPLOY"))
        ready = prepare_deploy(item, planned, record)
        with self.assertRaises(ValueError):
            start_deploy(item, planned, record)
        with self.assertRaises(ValueError):
            start_deploy(item, ready, record, command="not-allowlisted")


if __name__ == "__main__":
    unittest.main()
