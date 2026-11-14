"""Explicit CLI roots stay inside a real nested, non-Git child project."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import support_vibe_home as hermetic
from vibe_guide.cli import main, run_cli
from vibe_guide.protocols import VIBE_ENTRY_PROPOSAL_RELATIVE


class ExplicitNestedRootTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(hermetic.isolated_environment())
        self.enterContext(hermetic.override_env(VIBE_PROJECT_ROOT=None, GIT_CEILING_DIRECTORIES=None))
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.parent = Path(self.temp).resolve() / "parent"
        self.child = self.parent / "child"
        self.child.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.parent)], check=True, capture_output=True)
        (self.parent / "AGENTS.md").write_text("synthetic parent marker\n")
        self.old_state = self.parent / ".vibe" / "state.json"
        self.old_state.parent.mkdir()
        self.old_state.write_text('{"workflow_version":4,"sentinel":"parent-must-not-read"}')
        self.old_protocol = self.parent / VIBE_ENTRY_PROPOSAL_RELATIVE
        self.old_protocol.parent.mkdir(parents=True)
        self.old_protocol.write_text("synthetic immutable parent protocol\n")
        self.before = self.parent_files()
        self.addCleanup(self.assert_parent_unchanged)
        self.enterContext(patch("vibe_guide.cli._auto_start_daemon", return_value={"status": "isolated"}))

    def parent_files(self):
        return {str(p.relative_to(self.parent)): p.read_bytes()
                for p in (self.parent / ".vibe").rglob("*") if p.is_file()}

    def assert_parent_unchanged(self):
        self.assertEqual(self.parent_files(), self.before)

    @contextlib.contextmanager
    def no_parent_reads(self):
        original = Path.open
        forbidden = self.parent / ".vibe"

        def guarded(path, *args, **kwargs):
            if path.resolve().is_relative_to(forbidden):
                raise AssertionError("CLI attempted parent .vibe access: " + str(path))
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            yield

    def register_args(self, session="child-session"):
        return ["supervisor-register", "--run-id", "run-child", "--provider", "codex",
                "--session-id", session, "--host", "mac", "--json"]

    def invoke(self, args, source, entry, denied=False):
        arguments = list(args)
        env = {}
        if source == "flag":
            arguments += ["--project-root", str(self.child)]
        elif source == "env":
            env["VIBE_PROJECT_ROOT"] = str(self.child)
        output = io.StringIO()
        with self.no_parent_reads(), patch.dict(os.environ, env), contextlib.redirect_stdout(output):
            if denied:
                cwd = patch("os.getcwd", side_effect=PermissionError("synthetic getcwd denied"))
            else:
                cwd = patch("vibe_guide.cli.Path.cwd", return_value=self.parent)
            with cwd:
                if entry == "main":
                    code = main(arguments)
                    return code, json.loads(output.getvalue())
                result = run_cli(arguments, self.parent, environ=env)
                return result.exit_code, result.payload

    def registration(self, source, entry, denied, marker):
        if marker:
            (self.child / ".project-root").write_text("synthetic child marker\n")
        code, payload = self.invoke(self.register_args(), source, entry, denied)
        self.assertEqual(code, 0, payload)
        self.assertTrue((self.child / ".vibe" / "runs" / "run-child").is_dir(), payload)
        self.assertFalse((self.child / ".git").exists())

    def test_flag_precedes_invalid_environment_in_public_run_cli(self):
        with patch.dict(os.environ, {"VIBE_PROJECT_ROOT": "bad-relative"}), self.no_parent_reads():
            result = run_cli(self.register_args() + ["--project-root", str(self.child)],
                             self.parent, environ={"VIBE_PROJECT_ROOT": "bad-relative"})
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertTrue((self.child / ".vibe" / "runs" / "run-child").exists())

    def test_public_run_cli_uses_process_root_without_host_environment(self):
        with patch.dict(os.environ, {"VIBE_PROJECT_ROOT": str(self.child)}), self.no_parent_reads():
            result = run_cli(self.register_args(), self.parent)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertTrue((self.child / ".vibe" / "runs" / "run-child").exists())

    def test_install_boundary_keeps_request_root_without_installing(self):
        from vibe_guide.cli import run_install_or_upgrade
        class StopBeforeInstall(Exception):
            pass

        def check_boundary(request, paths):
            self.assertEqual(paths.root, self.child)
            self.assertEqual(request.project_root, self.child)
            raise StopBeforeInstall

        with self.no_parent_reads(), patch("vibe_guide.cli.run_install", check_boundary):
            with self.assertRaises(StopBeforeInstall):
                run_install_or_upgrade({"operation": "install", "project_root": str(self.child)})

    def test_auto_root_still_selects_parent_git(self):
        # Explicitly exercise legacy discovery using a read-only command.
        result = run_cli(["plan", "--print-protocol", "--json"], self.child)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertFalse((self.child / ".vibe").exists())
        from vibe_guide.paths import ProjectPaths
        self.assertEqual(ProjectPaths.from_cwd(self.child).root, self.parent)

    def test_invalid_public_roots_fail_before_parent_access(self):
        for source in ("flag", "env"):
            for value in ("relative/path", str(self.child / "missing")):
                with self.subTest(source=source, value=value), self.no_parent_reads():
                    args = ["plan", "--request", "继续", "--json"]
                    env = {"VIBE_PROJECT_ROOT": value} if source == "env" else {}
                    if source == "flag":
                        args += ["--project-root", value]
                    result = run_cli(args, self.parent, environ=env)
                    self.assertEqual(result.exit_code, 2, result.payload)
                    self.assertEqual(result.payload["status"], "usage_error")
        self.assertFalse((self.child / ".vibe").exists())

    def test_symlink_root_canonicalizes_to_selected_directory(self):
        alias = Path(self.temp) / "alias"
        alias.symlink_to(self.child, target_is_directory=True)
        with self.no_parent_reads():
            result = run_cli(self.register_args() + ["--project-root", str(alias)], self.parent)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertTrue((self.child / ".vibe" / "runs" / "run-child").exists())

    def test_vibe_symlink_cannot_escape_to_parent_state(self):
        (self.child / ".vibe").symlink_to(self.parent / ".vibe", target_is_directory=True)
        with self.no_parent_reads(), self.assertRaisesRegex(ValueError, "escapes"):
            run_cli(self.register_args() + ["--project-root", str(self.child)], self.parent)

    def test_relative_input_symlink_cannot_read_parent_state(self):
        (self.child / "facts.json").symlink_to(self.old_state)
        with self.no_parent_reads():
            result = run_cli(["attest", "--adapter", "codex", "--facts", "facts.json",
                              "--provenance", "synthetic", "--project-root", str(self.child), "--json"],
                             self.parent)
        self.assertNotEqual(result.exit_code, 0, result.payload)
        self.assertIn("escapes", result.payload.get("reason", ""))

    def test_main_bounded_output_evidence_stays_in_child(self):
        code, payload = self.invoke(self.register_args("x" * 24000), "flag", "main", True)
        self.assertEqual(code, 0, payload)
        evidence = Path(payload["output_evidence"]["path"])
        self.assertTrue(evidence.is_relative_to(self.child / ".vibe"), payload)
        self.assertTrue(evidence.is_file())


for _source in ("flag", "env"):
    for _entry in ("main", "run_cli"):
        for _denied in (False, True):
            for _marker in (False, True):
                def _case(self, source=_source, entry=_entry, denied=_denied, marker=_marker):
                    self.registration(source, entry, denied, marker)
                setattr(ExplicitNestedRootTests,
                        "test_register_%s_%s_denied%s_marker%s" % (_source, _entry, _denied, _marker), _case)

for _source in ("flag", "env"):
    for _command in ("plan", "resume", "monitor"):
        def _refresh(self, source=_source, command=_command):
            args = [command, "--json"]
            if command == "plan":
                args += ["--request", "继续"]
            self.invoke(args, source, "main", True)
            self.assertFalse((self.parent / ".vibe" / "runs").exists())
        setattr(ExplicitNestedRootTests, "test_refresh_%s_%s" % (_source, _command), _refresh)


if __name__ == "__main__":
    unittest.main()
