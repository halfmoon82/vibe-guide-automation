"""Contract: one vocabulary for provider names across the dispatch path.

`task_registry` decided whether to mirror a task id into the Codex-shaped
`threadId`/`hostId` aliases by testing `provider == "codex"`, while the runner
that writes those records, the adapter that reads them, and the manifest all
use `codex-app-visible`.  Production never took the registry's branch, so a
hand-written record was the only way to reach it — exactly the implicit
cross-module contract the project rules say must have a single source of truth
and a test, rather than the same string spelled two ways in two files.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ProviderVocabularyTests(unittest.TestCase):
    def test_codex_provider_name_has_one_definition(self):
        from vibe_guide.providers import CODEX_PROVIDER

        self.assertEqual(CODEX_PROVIDER, "codex-app-visible")
        manifest = json.loads((ROOT / "vibe_guide" / "adapters" / "manifests" / "codex.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["provider"], CODEX_PROVIDER)

    def test_no_module_spells_the_codex_provider_name_inline(self):
        """Every comparison must go through the shared constant."""
        from vibe_guide import providers

        offenders = []
        for path in sorted((ROOT / "vibe_guide").rglob("*.py")):
            if path.name == "providers.py":
                continue
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if 'provider == "codex"' in line or '"codex-app-visible"' in line:
                    offenders.append("{}:{}".format(path.relative_to(ROOT), number))
        self.assertEqual(offenders, [], "these lines must use providers.CODEX_PROVIDER")

    def test_registry_mirrors_codex_aliases_for_the_real_provider_name(self):
        """The alias mirroring must fire for the name production actually uses."""
        from vibe_guide.providers import CODEX_PROVIDER
        from vibe_guide.task_registry import TaskBinding

        binding = TaskBinding(
            provider=CODEX_PROVIDER,
            mode="visible",
            issue_id="node-a",
            role="developer",
            task_id="thread-1",
            host="mac",
            worktree="/tmp/wt",
            branch="feature/x",
            status_file="status.json",
            handoff_file="handoff.json",
            run_id="run-1",
            status="running",
            visible=True,
            generation=1,
        )
        self.assertEqual(binding.threadId, "thread-1")
        self.assertEqual(binding.hostId, "mac")

    def test_registry_leaves_aliases_alone_for_a_non_codex_provider(self):
        from vibe_guide.task_registry import TaskBinding

        binding = TaskBinding(
            provider="claude-code-visible",
            mode="visible",
            issue_id="node-a",
            role="developer",
            task_id="local_x",
            host="mac",
            worktree="/tmp/wt",
            branch="feature/x",
            status_file="status.json",
            handoff_file="handoff.json",
            run_id="run-1",
            status="running",
            visible=True,
            generation=1,
        )
        self.assertIsNone(binding.threadId)
        self.assertIsNone(binding.hostId)


if __name__ == "__main__":
    unittest.main()
