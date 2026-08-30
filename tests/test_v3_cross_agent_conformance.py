import unittest
from pathlib import Path

from vibe_guide.adapters.registry import SUPPORTED_ADAPTER_IDS
from vibe_guide.guidance import conformance_report, load_guidance_contract
from vibe_guide.cli import run_cli
from vibe_guide.adapters.task_provider import RepositoryTaskRouting, VisibleTaskProvider


class CrossAgentConformanceTests(unittest.TestCase):
    def test_all_supported_adapters_share_contract_and_semantics(self):
        report = conformance_report()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(set(report["adapters"]), set(SUPPORTED_ADAPTER_IDS))
        versions = {item["version"] for item in report["adapters"].values()}
        hashes = {item["contract_hash"] for item in report["adapters"].values()}
        contract = load_guidance_contract()
        self.assertEqual(versions, {contract["version"]})
        self.assertEqual(hashes, {contract["contract_hash"]})
        self.assertTrue(all(item["injection"]["verified"] for item in report["adapters"].values()))
        self.assertEqual(set(report["provider_injection"]), set(SUPPORTED_ADAPTER_IDS))
        self.assertTrue(all(item["verified"] for item in report["provider_injection"].values()))
        self.assertEqual(report["fixture"]["required_user_action"], "continue_planning")
        self.assertEqual(report["fixture"]["authorization_defaults"]["deploy"], "excluded")

    def test_conformance_detects_missing_guidance_even_when_cli_is_available(self):
        report = conformance_report(contract_path="/does/not/exist/canonical-contract.json")
        self.assertEqual(report["status"], "governance_pending")
        self.assertNotEqual(report.get("status"), "passed")

    def test_cli_exposes_read_only_conformance_command(self):
        result = run_cli(["conformance", "--json"], Path.cwd())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "passed")
        self.assertEqual(result.payload["command"], "conformance")

    def test_each_non_codex_provider_receives_structured_guidance_on_create(self):
        providers = {
            "claude-code-visible", "cursor-visible", "grok-visible", "workbuddy-visible",
            "kimi-code-visible", "deepseek-harness-visible",
        }
        for provider_name in sorted(providers):
            calls = []

            class Bridge:
                def create(self, role, issue_id, contract_path, *, prompt=None, guidance=None):
                    calls.append({"prompt": prompt, "guidance": guidance})
                    return {"task_id": "task-1", "host": "local"}

            provider = VisibleTaskProvider(
                provider_name,
                bridge=Bridge(),
                routing=RepositoryTaskRouting("project", "local", "worktree", "/tmp/v3-8", "codex/v3-8-rev5"),
            )
            binding = provider.create("developer", "V3-8", Path("contract.md"))
            self.assertEqual(binding.task_id, "task-1")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]["guidance"]["injection"]["verified"])
            self.assertEqual(calls[0]["guidance"]["contract_hash"], load_guidance_contract()["contract_hash"])


if __name__ == "__main__":
    unittest.main()
