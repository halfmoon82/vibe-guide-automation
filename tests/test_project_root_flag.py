"""--project-root / VIBE_PROJECT_ROOT: locate the project without the process cwd."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.cli import ProjectRootError, explicit_project_root, main
from vibe_guide.runners.provider_action import ProviderActionRunner

from tests.test_worker_push_delivery import GOOD, _store, _wait_request


def _deny_getcwd():
    raise PermissionError(1, "Operation not permitted")


def _run_main(argv, env=None):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env or {}, clear=False), \
            mock.patch("os.getcwd", side_effect=_deny_getcwd), \
            contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class ProjectRootFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        os.environ.pop("VIBE_PROJECT_ROOT", None)

    def test_worker_deliver_succeeds_when_getcwd_is_denied(self):
        _wait_request(_store(self.root))
        code, out = _run_main([
            "worker-deliver", "--project-root", str(self.root), "--generation", "1",
            "--run-id", "run-1", "--node", "n1", "--payload", json.dumps(GOOD), "--json",
        ])
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["consumed"])

    def test_supervisor_address_succeeds_when_getcwd_is_denied(self):
        code, out = _run_main([
            "supervisor-register", "--project-root", str(self.root), "--run-id", "run-1",
            "--provider", "codex", "--session-id", "T", "--host", "mac", "--json",
        ])
        self.assertEqual(code, 0, out)
        code, out = _run_main([
            "supervisor-address", "--project-root", str(self.root), "--run-id", "run-1",
            "--node", "n1", "--role", "developer", "--generation", "1", "--json",
        ])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["wake"]["signal"],
                         "vibe-wake run=run-1 node=n1 role=developer generation=1")

    def test_env_var_alone_works(self):
        _wait_request(_store(self.root))
        code, out = _run_main([
            "worker-deliver", "--generation", "1", "--run-id", "run-1", "--node", "n1",
            "--payload", json.dumps(GOOD), "--json",
        ], env={"VIBE_PROJECT_ROOT": str(self.root)})
        self.assertEqual(code, 0, out)

    def test_flag_beats_env_var(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        env = {"VIBE_PROJECT_ROOT": str(other)}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(explicit_project_root(["status", "--project-root", str(self.root)]), self.root)
            self.assertEqual(explicit_project_root(["status"]), other)
        # a bad env value is ignored once the flag is given
        with mock.patch.dict(os.environ, {"VIBE_PROJECT_ROOT": "relative/x"}):
            self.assertEqual(explicit_project_root(["--project-root", str(self.root)]), self.root)

    def test_relative_path_is_refused(self):
        for source in ("flag", "env"):
            with self.subTest(source=source):
                if source == "flag":
                    code, out = _run_main(["status", "--project-root", "rel/dir", "--json"])
                else:
                    code, out = _run_main(["status", "--json"], env={"VIBE_PROJECT_ROOT": "rel/dir"})
                self.assertNotEqual(code, 0)
                self.assertIn("绝对路径", out)

    def test_missing_directory_is_reported(self):
        missing = str(self.root / "nope")
        code, out = _run_main(["status", "--project-root", missing, "--json"])
        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "usage_error")
        self.assertIn("不存在", payload["reason"])
        with self.assertRaises(ProjectRootError):
            explicit_project_root(["--project-root", missing])

    def test_no_option_keeps_cwd_behaviour(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIBE_PROJECT_ROOT", None)
            self.assertIsNone(explicit_project_root(["status"]))

    def test_dispatch_prompts_carry_project_root(self):
        prompt = ProviderActionRunner._continuation_prompt(
            {"node_id": "n1", "role": "developer", "generation": 2, "run_id": "run-1"}, self.root)
        self.assertIn("vibe worker-deliver --project-root {} --run-id run-1".format(self.root), prompt)
        self.assertIn("vibe supervisor-address --project-root {} --run-id run-1".format(self.root), prompt)

    def test_doctor_runs_when_getcwd_is_denied(self):
        code, out = _run_main(["doctor", "--project-root", str(self.root), "--json"])
        self.assertEqual(json.loads(out)["command"], "doctor")  # a diagnosis, not a getcwd crash

    def test_unknown_tilde_user_is_a_usage_error_not_a_crash(self):
        code, out = _run_main(["status", "--project-root", "~vibe-no-such-user-xyz", "--run-id", "run-1", "--json"])
        self.assertEqual(code, 2, out)

    def test_install_request_with_a_root_never_reads_cwd(self):
        from vibe_guide import cli
        seen = {}

        class Stop(Exception):
            pass

        def capture(request, paths):
            seen["root"] = request.project_root
            raise Stop

        with mock.patch("os.getcwd", side_effect=_deny_getcwd), mock.patch.object(cli, "run_install", capture):
            try:
                cli.run_install_or_upgrade({"operation": "install", "project_root": str(self.root)})
            except Stop:
                pass
            except PermissionError:
                self.fail("cwd was read although the request carried a project_root")
        self.assertEqual(Path(seen["root"]), self.root)

    def test_abbreviated_flag_is_honoured_not_ignored(self):
        self.assertEqual(explicit_project_root(["status", "--project-r", str(self.root)]), self.root)

    def test_empty_flag_and_file_path_are_refused(self):
        with self.assertRaises(ProjectRootError):
            explicit_project_root(["status", "--project-root", ""])
        afile = self.root / "f.txt"
        afile.write_text("x")
        with self.assertRaises(ProjectRootError) as ctx:
            explicit_project_root(["status", "--project-root", str(afile)])
        self.assertIn("不是目录", str(ctx.exception))

    def test_empty_env_var_still_means_unset(self):
        self.assertIsNone(explicit_project_root(["status"], {"VIBE_PROJECT_ROOT": ""}))

    def test_parse_failure_does_not_print_usage_twice(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(explicit_project_root(["status", "--no-such-option"]))
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
