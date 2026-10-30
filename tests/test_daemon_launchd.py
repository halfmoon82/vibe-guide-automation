"""launchd keep-alive, exercised only against a fake ``launchctl``.

Nothing here may load a real LaunchAgent: the runner is injected, the plist
directory is a temporary one, and one test pins that the real directory is
never written.
"""
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vibe_guide import launchd_keepalive as lk
from vibe_guide.paths import ProjectPaths

RUN = "run-abc12345"
COMMAND = ["/usr/bin/python3", "-m", "vibe_guide", "daemon", "run", "--plan", "p1", "--run-id", RUN]


class FakeLaunchctl:
    def __init__(self, bootstrap=0, bootout=0):
        self.calls = []
        self.codes = {"bootstrap": bootstrap, "bootout": bootout}
        self.plists = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "bootstrap":
            self.plists.append(Path(argv[3]).read_bytes())
        return SimpleNamespace(returncode=self.codes.get(verb, 0), stdout="", stderr="")

    def verbs(self):
        return [call[1] for call in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = ProjectPaths(root, vibe_home=root / "home")
        self.agents = root / "LaunchAgents"
        self.agents.mkdir()
        self.fake = FakeLaunchctl()

    def install(self, **kwargs):
        kwargs.setdefault("runner", self.fake)
        return lk.install(self.paths, RUN, COMMAND, agents_dir=self.agents, platform="darwin", uid=501, **kwargs)

    def uninstall(self, **kwargs):
        kwargs.setdefault("runner", self.fake)
        return lk.uninstall(self.paths, RUN, agents_dir=self.agents, platform="darwin", uid=501, **kwargs)


class InstallTests(Base):
    def test_fake_launchctl_receives_a_plist_that_passes_validation(self):
        result = self.install()
        self.assertEqual(result["status"], "loaded")
        self.assertEqual(self.fake.calls[-1][:3], [lk.LAUNCHCTL, "bootstrap", "gui/501"])
        self.assertEqual(lk.validate_plist(self.fake.plists[-1], RUN), [])
        data = plistlib.loads(self.fake.plists[-1])
        self.assertEqual(data["Label"], lk.LABEL_PREFIX + RUN)
        self.assertEqual(data["ProgramArguments"], COMMAND)
        self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})

    def test_plist_is_written_only_under_the_given_directory(self):
        self.install()
        self.assertEqual([p.name for p in self.agents.iterdir()], [lk.LABEL_PREFIX + RUN + ".plist"])

    def test_default_directory_is_never_touched_when_one_is_injected(self):
        with mock.patch.object(lk, "default_agents_dir", side_effect=AssertionError("real dir used")):
            self.install()
            self.uninstall()

    def test_real_launchctl_is_not_run_without_an_injected_runner(self):
        with mock.patch.object(lk.subprocess, "run", side_effect=AssertionError("real launchctl")):
            for fn in (self.install, self.uninstall):
                with self.assertRaises(AssertionError):
                    fn(runner=lk._run_launchctl)

    def test_bootstrap_failure_removes_the_plist_and_reports(self):
        self.fake = FakeLaunchctl(bootstrap=5)
        result = self.install()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(list(self.agents.iterdir()), [])

    def test_launchctl_missing_is_reported_not_raised(self):
        def missing(argv):
            raise FileNotFoundError(argv[0])
        result = self.install(runner=missing)
        self.assertEqual(result["status"], "failed")
        self.assertIn("FileNotFoundError", result["reason"])

    def test_relative_program_path_is_refused_before_anything_is_written(self):
        result = lk.install(self.paths, RUN, ["python3", "-m", "vibe_guide", "daemon", "run", RUN],
                            runner=self.fake, agents_dir=self.agents, platform="darwin", uid=501)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(list(self.agents.iterdir()), [])

    def test_bad_run_id_is_rejected(self):
        with self.assertRaises(ValueError):
            lk.label("../evil")


class UninstallTests(Base):
    def test_unload_then_delete(self):
        self.install()
        result = self.uninstall()
        self.assertEqual(result["status"], "uninstalled")
        self.assertEqual(self.fake.verbs()[-1], "bootout")
        self.assertEqual(list(self.agents.iterdir()), [])

    def test_nothing_installed_is_a_no_op(self):
        result = self.uninstall()
        self.assertEqual(result["status"], "not_installed")
        self.assertEqual(self.fake.calls, [])

    def test_failed_unload_keeps_the_plist(self):
        self.install()
        self.fake.codes["bootout"] = 5
        result = self.uninstall()
        self.assertEqual(result["status"], "unload_failed")
        self.assertEqual(len(list(self.agents.iterdir())), 1)

    def test_job_already_gone_still_deletes_the_plist(self):
        self.install()
        self.fake.codes["bootout"] = 113
        self.assertEqual(self.uninstall()["status"], "uninstalled")


class PlatformTests(Base):
    def test_other_platforms_use_the_same_entry_and_report_unsupported(self):
        for platform in ("linux", "win32"):
            result = lk.install(self.paths, RUN, COMMAND, runner=self.fake, agents_dir=self.agents,
                                platform=platform, uid=501)
            self.assertEqual(result["status"], "unsupported")
            self.assertEqual(result["verification"], "未验证")
        self.assertEqual(self.fake.calls, [])

    def test_every_platform_is_marked_unverified(self):
        self.assertEqual(set(lk.PLATFORM_VERIFICATION.values()), {"未验证"})


class ValidateTests(Base):
    def test_rejects_broken_plists(self):
        good = plistlib.loads(lk.build_plist(self.paths, RUN, COMMAND))
        cases = {
            "not a plist": b"garbage",
            "wrong label": {**good, "Label": "com.vibeguide.daemon.other"},
            "no keep-alive": {k: v for k, v in good.items() if k != "KeepAlive"},
            "always restart": {**good, "KeepAlive": True},
            "relative program": {**good, "ProgramArguments": ["python3", "daemon", "run", RUN]},
            "not this daemon": {**good, "ProgramArguments": ["/bin/sh", "-c", "true"]},
        }
        for name, value in cases.items():
            data = value if isinstance(value, bytes) else plistlib.dumps(value)
            self.assertTrue(lk.validate_plist(data, RUN), name)


if __name__ == "__main__":
    unittest.main()
