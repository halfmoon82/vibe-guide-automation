import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide.engine_attestation import (
    create_engine_attestation,
    validate_engine_attestation,
)


class EngineAttestationTests(unittest.TestCase):
    def _create(self, **overrides):
        values = {
            "plan_id": "p",
            "plan_revision": 1,
            "execution_engine": "vibeguide_monitor",
            "engine_mode": "dag",
            "provider": "codex",
            "capability_facts": {"codex.worktree": True},
            "provenance": "live:test",
            "now": "2026-09-06T00:00:00Z",
        }
        values.update(overrides)
        return create_engine_attestation(**values)

    def test_create_attestation_has_stable_digest_and_evidence_ref(self):
        result = self._create()
        self.assertTrue(result["evidence_ref"].startswith("engine-attestation:"))
        self.assertEqual(len(result["digest"]), 64)
        self.assertEqual(result, self._create(capability_facts={"codex.worktree": True}))

    def test_validate_rejects_wrong_revision(self):
        attestation = self._create()
        with self.assertRaisesRegex(ValueError, "revision"):
            validate_engine_attestation(attestation, "p", 2)

    def test_contract_requires_monitor_dag_identity(self):
        with self.assertRaisesRegex(ValueError, "execution engine"):
            self._create(execution_engine="other")
        with self.assertRaisesRegex(ValueError, "engine mode"):
            self._create(engine_mode="worker")

    def test_provider_attestation_does_not_grant_external_permission(self):
        """Engineering engine identity must not be treated as external approval."""
        attestation = self._create(provider="codex")
        self.assertEqual(attestation["provider"], "codex")
        self.assertNotIn("permission_granted", attestation)

    def test_contract_requires_non_empty_text_and_boolean_facts(self):
        for field in ("plan_id", "provider", "provenance"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self._create(**{field: " "})
        with self.assertRaisesRegex(ValueError, "boolean"):
            self._create(capability_facts={"codex.worktree": "yes"})

    def test_validate_rejects_tampered_digest_or_expected_digest(self):
        attestation = self._create()
        tampered = dict(attestation)
        tampered["provider"] = "claude"
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_engine_attestation(tampered, "p", 1)
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_engine_attestation(attestation, "p", 1, expected_digest="0" * 64)

    def test_validate_rejects_expired_or_future_attestation(self):
        old = self._create(now="2020-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "expired"):
            validate_engine_attestation(old, "p", 1, now="2026-09-06T00:00:00Z")
        future = self._create(now="2026-09-06T01:00:00Z")
        with self.assertRaisesRegex(ValueError, "future"):
            validate_engine_attestation(future, "p", 1, now="2026-09-06T00:00:00Z")

    def test_monitor_rejects_missing_attestation_before_provider_call(self):
        """A verified-looking card cannot bypass the on-disk engine evidence gate."""
        from vibe_guide.monitor import Monitor
        from vibe_guide.paths import ProjectPaths

        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            plan = SimpleNamespace(
                complexity_band="complex", plan_id="p", version=1,
            )
            monitor = Monitor(paths, plan, [])
            record = SimpleNamespace(
                execution_engine="vibeguide_monitor",
                engine_mode="dag",
                engine_evidence_ref="engine-attestation:" + "a" * 16,
                dag_revision=1,
                agent_id="codex",
                explicit_execution_mode_override=None,
            )

            with self.assertRaisesRegex(PermissionError, "execution_engine_unverified"):
                monitor._execution_engine_binding(record)

    def test_monitor_rejects_attestation_from_another_plan(self):
        from vibe_guide.monitor import Monitor
        from vibe_guide.paths import ProjectPaths

        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            plan = SimpleNamespace(
                complexity_band="complex", plan_id="p", version=1,
            )
            monitor = Monitor(paths, plan, [])
            attestation = self._create(plan_id="other")
            target = paths.vibe / "plans" / "p"
            target.mkdir(parents=True)
            (target / "engine-attestation.json").write_text(
                json.dumps(attestation), encoding="utf-8"
            )
            record = SimpleNamespace(
                execution_engine="vibeguide_monitor",
                engine_mode="dag",
                engine_evidence_ref=attestation["evidence_ref"],
                dag_revision=1,
                agent_id="codex",
                explicit_execution_mode_override=None,
            )

            with self.assertRaisesRegex(PermissionError, "execution_engine_unverified"):
                monitor._execution_engine_binding(record)

    def test_monitor_rejects_symlink_attestation(self):
        """Attestation files must be regular files, preventing path redirection."""
        from vibe_guide.monitor import Monitor
        from vibe_guide.paths import ProjectPaths

        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            plan = SimpleNamespace(complexity_band="complex", plan_id="p", version=1)
            monitor = Monitor(paths, plan, [])
            attestation = self._create()
            target = paths.vibe / "plans" / "p"
            target.mkdir(parents=True)
            real = paths.vibe / "real-attestation.json"
            real.write_text(json.dumps(attestation), encoding="utf-8")
            link = target / "engine-attestation.json"
            try:
                os.symlink(real, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            record = SimpleNamespace(
                execution_engine="vibeguide_monitor", engine_mode="dag",
                engine_evidence_ref=attestation["evidence_ref"], dag_revision=1,
                agent_id="codex",
                explicit_execution_mode_override=None,
            )
            with self.assertRaisesRegex(PermissionError, "execution_engine_unverified"):
                monitor._execution_engine_binding(record)

    def test_complex_plan_publication_persists_verified_engine_attestation(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.cli import run_cli
        from vibe_guide.paths import ProjectPaths

        fixture = Path(__file__).parent / "fixtures" / "e2e-project"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            shutil.copytree(fixture, root)
            self.assertEqual(run_cli(["init", "--confirm", "--json"], root).exit_code, 0)
            ProviderActionStore(ProjectPaths(root)).publish_capabilities(
                "codex",
                {"codex.shell": True, "codex.subprocess": True, "codex.worktree": True,
                 "codex.visible_task.create": True, "codex.visible_task.enter": True,
                 "codex.visible_task.resume": True, "codex.visible_task.wait": True},
                "codex-app-session-bridge",
            )
            source = json.loads((root / "plan-source.json").read_text())
            source["capabilities"] = {"agent_id": "codex", "shell": True, "subprocess": True,
                                       "worktree": True, "background": True, "session_resume": True,
                                       "level": "full"}
            source["project_id"] = "project-fixture"
            source["complexity_band"] = "complex"
            source["integration_contract"] = {
                "iteration_context": {"kind": "iteration", "based_on": "V4"},
                "compatibility_scope": ["V4 API"],
                "agentsmd_acceptance_refs": ["AGENTS.md#8"],
                "integration_acceptance_contract": {"checks": ["all"]},
                "unverified_or_excluded": ["provider"],
            }
            source_path = root / "v42-source.json"
            source_path.write_text(json.dumps(source))
            result = run_cli(["plan", "--request", "设计并实现两个契约兼容的并行节点并完成独立审查", "--plan-id", "v42-attestation",
                              "--s1", "4,4,4,4,4", "--node-spec", source_path.name, "--json"], root)
            self.assertEqual(result.exit_code, 0, result.text)
            card = result.payload["authorization_card"]
            self.assertTrue(card["engine_evidence_ref"].startswith("engine-attestation:"))
            attestation = json.loads((root / ".vibe" / "plans" / "v42-attestation" / "engine-attestation.json").read_text())
            self.assertEqual(attestation["plan_id"], "v42-attestation")
            self.assertEqual(card["engine_evidence_ref"], attestation["evidence_ref"])


if __name__ == "__main__":
    unittest.main()
