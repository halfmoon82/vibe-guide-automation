import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.guidance import GuidanceContractError, conformance_report, guidance_for_stage, load_guidance_contract
from vibe_guide.adapters.task_provider import CodexAppBridge, ProviderUnavailable, RepositoryTaskRouting, VisibleTaskProvider
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.cli import _governance_pending_result


class GuidanceContractTests(unittest.TestCase):
    def test_registry_loader_keeps_custom_path_compatibility(self):
        import vibe_guide.adapters.registry as registry
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(load_guidance_contract()), encoding="utf-8")
            loaded = registry.load_guidance_contract(path)
            self.assertEqual(loaded["contract_hash"], load_guidance_contract()["contract_hash"])

    def test_registry_loader_signature_survives_reverse_import_order(self):
        import inspect
        import vibe_guide.runners.provider_action  # noqa: F401
        import vibe_guide.adapters.registry as registry
        self.assertEqual(str(inspect.signature(registry.load_guidance_contract)), "(path: Optional[pathlib.Path] = None)")

    def test_canonical_contract_is_versioned_hashed_and_has_required_semantics(self):
        contract = load_guidance_contract()
        self.assertEqual(contract["version"], "v3")
        self.assertRegex(contract["contract_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(guidance_for_stage(contract, "prd_approved", "approved")["required_user_action"], "continue_planning")
        self.assertEqual(contract["authorization_defaults"]["deploy"], "excluded")
        self.assertIn("create_worker", contract["forbidden_automatic_actions"])

    def test_invalid_or_missing_contract_is_governance_pending_with_remediation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canonical-contract.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(GuidanceContractError):
                load_guidance_contract(path)
            report = conformance_report(contract_path=path)
            self.assertEqual(report["status"], "governance_pending")
            self.assertTrue(report["remediation"])

    def test_hash_mismatch_is_rejected(self):
        contract = load_guidance_contract()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canonical-contract.json"
            contract["contract_hash"] = "0" * 64
            path.write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaisesRegex(GuidanceContractError, "hash mismatch"):
                load_guidance_contract(path)

    def test_provider_loads_guidance_before_visible_task_creation(self):
        provider = VisibleTaskProvider(
            "codex-app-visible",
            guidance_loader=lambda: (_ for _ in ()).throw(GuidanceContractError("missing")),
        )
        with self.assertRaisesRegex(ProviderUnavailable, "governance_pending"):
            provider.guidance_context()

    def test_guidance_failure_has_structured_governance_pending_state(self):
        provider = VisibleTaskProvider(
            "codex-app-visible",
            guidance_loader=lambda: (_ for _ in ()).throw(GuidanceContractError("drift")),
        )
        with self.assertRaises(ProviderUnavailable) as raised:
            provider.guidance_context()
        self.assertEqual(raised.exception.status, "governance_pending")
        self.assertTrue(raised.exception.remediation)

    def test_cli_preserves_structured_governance_pending_state(self):
        error = ProviderUnavailable("governance_pending: drift", status="governance_pending", reason="drift", remediation=("rerun conformance",))
        result = _governance_pending_result("monitor", error, True)
        self.assertEqual(result.payload["status"], "governance_pending")
        self.assertEqual(result.payload["remediation"], ["rerun conformance"])
        resume = _governance_pending_result("resume", error, True)
        self.assertEqual(resume.payload["status"], "governance_pending")

    def test_provider_injects_structured_guidance_into_create_prompt(self):
        calls = []

        bridge = CodexAppBridge(
            create_thread=lambda request: (calls.append(request) or {"task_id": "thread-1", "host": "local"}),
            navigate_to_codex_page=lambda request: None,
            send_message_to_thread=lambda request: None,
            wait_threads=lambda request: {"polls": []},
        )

        provider = VisibleTaskProvider(
            "codex-app-visible",
            bridge=bridge,
            routing=RepositoryTaskRouting("project", "local", "worktree", "/tmp/v3-8", "codex/v3-8-rev5"),
            prompt_factory=lambda *_: "base prompt",
        )
        binding = provider.create("developer", "V3-8", Path("contract.md"))
        self.assertEqual(binding.task_id, "thread-1")
        self.assertEqual(len(calls), 1)
        self.assertIn("Guidance Contract", calls[0]["prompt"])
        self.assertIn(load_guidance_contract()["contract_hash"], calls[0]["prompt"])

    def test_provider_action_runner_fails_closed_before_create_when_guidance_drifts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = type("Paths", (), {"vibe": root / ".vibe", "vibe_dir": root / ".vibe", "resolve_vibe_path": lambda self, name: root / ".vibe" / name})()
            runner = ProviderActionRunner(paths, "codex", "codex-app-visible")
            contract = {"node_id": "V3-8", "role": "developer", "generation": 1, "project_id": "project", "branch": "codex/v3-8-rev5"}
            with patch("vibe_guide.adapters.task_provider.load_guidance_contract", side_effect=ValueError("drift")):
                with self.assertRaisesRegex(RuntimeError, "governance_pending") as raised:
                    runner.task_binding(contract, root, "run-1", "running")
                self.assertEqual(getattr(raised.exception, "status", None), "governance_pending")
            self.assertFalse((root / ".vibe" / "provider-actions" / "requests").exists())


if __name__ == "__main__":
    unittest.main()
