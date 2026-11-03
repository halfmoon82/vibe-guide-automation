"""The hermetic VIBE_HOME / host-identity support (#378)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import support_vibe_home as hermetic
from vibe_guide import model_catalog
from vibe_guide.scanner import claude_code_session
from vibe_guide.supervisor import caller_session_ids

ROOT = Path(__file__).resolve().parents[1]
HOSTILE = {
    "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_CODE_HOST_SESSION_ID": "s2",
    "CODEX_THREAD_ID": "t1", "CODEBUDDY_SESSION_ID": "w1",
}


class IsolationTests(unittest.TestCase):
    def test_vibe_home_is_a_fixed_minimal_config(self):
        with hermetic.override_env(**HOSTILE), hermetic.isolated_environment() as home:
            self.assertEqual(os.environ["VIBE_HOME"], str(home))
            config = json.loads((home / "config.json").read_text())
            self.assertIn("claude_bg", config)
            self.assertNotIn("model_map", config)
            self.assertIsNone(model_catalog.load_model_map(model_catalog.vibe_home_path()))

    def test_host_identity_is_cleared_and_restored(self):
        with hermetic.override_env(**HOSTILE):
            with hermetic.isolated_environment():
                self.assertFalse(claude_code_session())
                self.assertEqual(caller_session_ids(), ())
                for name in HOSTILE:
                    self.assertNotIn(name, os.environ)
            self.assertEqual(os.environ["CLAUDECODE"], "1")
            self.assertEqual(os.environ["CODEX_THREAD_ID"], "t1")

    def test_case_can_declare_its_own_host_and_config(self):
        with hermetic.isolated_environment() as home:
            with hermetic.override_env(CLAUDECODE="1", CODEX_THREAD_ID="t9"):
                self.assertTrue(claude_code_session())
                self.assertEqual(caller_session_ids(), ("t9",))
            self.assertFalse(claude_code_session())
            hermetic.write_config({"model_map": {"codex": {"deep": ["x"]}}}, home)
            self.assertIsNotNone(model_catalog.load_model_map(home))

    def test_tier_tests_pass_under_a_hostile_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            home.mkdir()
            (home / "config.json").write_text(json.dumps({
                "model_map": {"claude-code": {"deep": ["zzz-deep"], "standard": ["zzz-std"]}}}))
            env = dict(os.environ, VIBE_HOME=str(home), **HOSTILE)
            for how in (["-s", "tests", "-p", "test_daemon_short_session_tier.py"],
                        ["tests.test_daemon_short_session_tier"]):
                cmd = [sys.executable, "-m", "unittest"] + (["discover"] + how if how[0] == "-s" else how)
                result = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])


if __name__ == "__main__":
    unittest.main()
