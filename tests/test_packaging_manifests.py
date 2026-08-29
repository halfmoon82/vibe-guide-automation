import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from vibe_guide.adapters.registry import AdapterRegistry, ManifestError, SUPPORTED_ADAPTER_IDS, inspect_manifest_source


def manifest(adapter_id):
    return {
        "id": adapter_id,
        "display_name": adapter_id,
        "provider": "codex-app-visible",
        "schema_version": 1,
        "source": {"ref": "https://github.com/example/vibe-guide", "commit": "a" * 40},
    }


class PackagingManifestTests(unittest.TestCase):
    def test_default_registry_has_verified_source_metadata(self):
        registry = AdapterRegistry()
        integrity = registry.integrity()
        self.assertEqual(integrity["status"], "verified")
        self.assertTrue(integrity["source_ref"].startswith("https://"))
        self.assertRegex(integrity["source_commit"], r"^[0-9a-f]{40}$")
        for adapter_id in SUPPORTED_ADAPTER_IDS:
            source = registry.get(adapter_id).raw["source"]
            self.assertEqual(set(source), {"ref", "commit"})
            self.assertEqual(source["ref"], integrity["source_ref"])
            self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")

    def test_empty_manifest_directory_is_governance_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ManifestError):
                AdapterRegistry(Path(directory))

    def test_source_integrity_rejects_temp_and_missing_commit(self):
        self.assertEqual(inspect_manifest_source("/tmp/build", "a" * 40)["status"], "blocked_unknown")
        self.assertEqual(inspect_manifest_source("https://github.com/example/vibe-guide", "a" * 40)["status"], "verified")
        with self.assertRaises(ValueError):
            inspect_manifest_source("https://github.com/example/vibe-guide", "")

    def test_local_and_relative_sources_are_not_verified(self):
        for source in ("relative/path", "/Users/foo/src", "file:///Users/foo/src"):
            self.assertNotEqual(inspect_manifest_source(source, "a" * 40)["status"], "verified")

    def test_registry_adapter_retains_detect_api(self):
        registry = AdapterRegistry.from_manifests([manifest("codex")], require_complete=False)
        self.assertTrue(hasattr(registry.get("codex"), "detect"))

    def test_each_adapter_has_its_own_provider_route(self):
        registry = AdapterRegistry()
        providers = {name: registry.get(name).provider for name in sorted(SUPPORTED_ADAPTER_IDS)}
        self.assertEqual(providers, {
            "codex": "codex-app-visible",
            "claude-code": "claude-code-visible",
            "cursor": "cursor-visible",
            "grok": "grok-visible",
            "workbuddy": "workbuddy-visible",
            "kimi-code": "kimi-code-visible",
            "deepseek-harness": "deepseek-harness-visible",
        })

    def test_manifest_provider_must_match_adapter_route(self):
        invalid = manifest("cursor")
        with self.assertRaises(ManifestError):
            AdapterRegistry.from_manifests([invalid], require_complete=False)

    def test_manifest_directory_loads_structured_json_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.yaml"
            path.write_text(json.dumps(manifest("codex")), encoding="utf-8")
            registry = AdapterRegistry.from_manifests([manifest("codex")], require_complete=False,
                                                       source_ref="https://github.com/example/vibe-guide",
                                                       source_commit="a" * 40)
            self.assertEqual(registry.ids, ("codex",))
            self.assertEqual(registry.integrity()["status"], "verified")

    def test_clean_wheel_and_sdist_retain_source_metadata(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "project"
            shutil.copytree(
                project,
                checkout,
                ignore=shutil.ignore_patterns(".git", ".vibe", "__pycache__", "*.egg-info", "build"),
            )
            dist = Path(directory) / "dist"
            dist.mkdir()
            subprocess.run([sys.executable, "setup.py", "sdist", "--dist-dir", str(dist)], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(dist)], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            wheel = next(dist.glob("*.whl"))
            sdist = next(dist.glob("*.tar.gz"))
            installed = Path(directory) / "installed"
            subprocess.run([sys.executable, "-m", "pip", "install", str(wheel), "--no-deps", "--target", str(installed)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            probe = subprocess.run(
                [sys.executable, "-c", "import json; from vibe_guide.adapters.registry import AdapterRegistry; print(json.dumps(AdapterRegistry().integrity()))"],
                env={**os.environ, "PYTHONPATH": str(installed)},
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            installed_integrity = json.loads(probe.stdout)
            self.assertEqual(installed_integrity["status"], "verified")
            self.assertTrue(installed_integrity["source_ref"].startswith("https://"))
            self.assertRegex(installed_integrity["source_commit"], r"^[0-9a-f]{40}$")
            for archive in (wheel,):
                with zipfile.ZipFile(archive) as handle:
                    names = handle.namelist()
                    manifest_names = sorted(name for name in names if "adapters/manifests/" in name and name.endswith(".yaml"))
                    self.assertEqual(len(manifest_names), len(SUPPORTED_ADAPTER_IDS))
                    for name in manifest_names:
                        source = json.loads(handle.read(name))["source"]
                        self.assertTrue(source["ref"].startswith("https://"))
                        self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
            with tarfile.open(sdist) as handle:
                names = handle.getnames()
                manifest_names = sorted(name for name in names if "adapters/manifests/" in name and name.endswith(".yaml"))
                self.assertEqual(len(manifest_names), len(SUPPORTED_ADAPTER_IDS))


if __name__ == "__main__":
    unittest.main()
