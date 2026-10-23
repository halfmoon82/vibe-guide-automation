"""doctor 的「装的是不是安装版」判定（#257）。

三种情形都靠注入路径构造，不读本机 pipx / site-packages 的真实状态。
用 tempfile 而不是 pytest 的 tmp_path：沙箱里 pytest basetemp 目录已存在时
mkdir 会报 EEXIST，与这条检查无关却会让测试假红。
"""
import sysconfig
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import doctor as doctor_module
from vibe_guide.doctor import install_source_facts
from vibe_guide.scanner import scan_project
from vibe_guide.paths import ProjectPaths


def _package_file(root, name='vibe_guide'):
    return Path(root) / name / 'doctor.py'


class InstallSourceFactsTests(unittest.TestCase):
    def test_source_tree_import_is_not_installed_and_hints_clean_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cwd = root / 'repo'
            site = root / 'venv' / 'lib' / 'site-packages'
            package = _package_file(cwd)

            facts = install_source_facts(
                cwd=cwd, package_file=package, site_packages=[site]
            )

            self.assertIs(facts['in_cwd'], True)
            self.assertIs(facts['in_site_packages'], False)
            self.assertIs(facts['installed'], False)
            self.assertIn('源码树', facts['hint'])
            self.assertIn('/tmp', facts['hint'])
            self.assertEqual(facts['package_path'], str(package.resolve()))

    def test_site_packages_import_is_installed_and_silent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cwd = root / 'clean'
            site = root / 'venv' / 'lib' / 'site-packages'
            package = _package_file(site)

            facts = install_source_facts(
                cwd=cwd, package_file=package, site_packages=[site]
            )

            self.assertIs(facts['in_cwd'], False)
            self.assertIs(facts['in_site_packages'], True)
            self.assertIs(facts['installed'], True)
            self.assertIsNone(facts['hint'])

    def test_import_outside_cwd_and_site_packages_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cwd = root / 'clean'
            site = root / 'venv' / 'lib' / 'site-packages'
            package = _package_file(root / 'somewhere-else')

            facts = install_source_facts(
                cwd=cwd, package_file=package, site_packages=[site]
            )

            self.assertIs(facts['in_cwd'], False)
            self.assertIs(facts['in_site_packages'], False)
            self.assertIs(facts['installed'], False)
            self.assertIn('site-packages', facts['hint'])
            self.assertIn('/tmp', facts['hint'])

    def test_source_tree_next_to_a_project_venv_is_still_source(self):
        """源码树旁边有 .venv 时，从源码树加载的仍然是源码树，不是安装版。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cwd = root / 'repo'
            site = cwd / '.venv' / 'lib' / 'site-packages'
            package = _package_file(cwd)

            facts = install_source_facts(
                cwd=cwd, package_file=package, site_packages=[site]
            )

            self.assertIs(facts['in_cwd'], True)
            self.assertIs(facts['installed'], False)
            self.assertIn('源码树', facts['hint'])

    def test_site_packages_under_cwd_is_still_installed(self):
        """在 $HOME 下跑 doctor，pipx venv 也在 $HOME 下：仍是安装版。"""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            site = home / '.local' / 'pipx' / 'venvs' / 'vibe-guide' / 'lib' / 'site-packages'
            package = _package_file(site)

            facts = install_source_facts(
                cwd=home, package_file=package, site_packages=[site]
            )

            self.assertIs(facts['in_cwd'], True)
            self.assertIs(facts['in_site_packages'], True)
            self.assertIs(facts['installed'], True)
            self.assertIsNone(facts['hint'])

    def test_default_package_file_follows_module_file(self):
        """默认取本模块文件（与 vibe_guide/__init__.py 同目录），可替换。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fake = _package_file(root / 'repo')
            with mock.patch.object(doctor_module, '__file__', str(fake)):
                facts = install_source_facts(
                    cwd=root / 'repo', site_packages=[root / 'site-packages']
                )

            self.assertEqual(facts['package_path'], str(fake.resolve()))
            self.assertIs(facts['in_cwd'], True)

    def test_doctor_reports_install_facts_without_changing_readiness(self):
        """提示只进 facts：不改 ok/status，也不进 issues 或 warnings。"""
        stub = {
            'package_path': '/repo/vibe_guide/doctor.py',
            'in_cwd': True,
            'in_site_packages': False,
            'installed': False,
            'hint': 'vibe_guide 从当前目录的源码树加载（/repo/vibe_guide/doctor.py）',
        }
        with mock.patch.object(doctor_module, 'install_source_facts', lambda: stub):
            with tempfile.TemporaryDirectory() as d:
                report = doctor_module.doctor(
                    scan_project(ProjectPaths.from_cwd(Path(d)))
                )

        self.assertEqual(report.facts['install'], stub)
        self.assertNotIn(stub['hint'], report.issues)
        self.assertNotIn(stub['hint'], report.warnings)
        self.assertEqual(report.ok, not report.issues)

    def test_default_site_packages_comes_from_interpreter(self):
        """不注入时，site-packages 集合来自当前解释器（不要求本机真装了包）。"""
        with tempfile.TemporaryDirectory() as d:
            facts = install_source_facts(
                cwd=Path(d), package_file=_package_file(Path(d) / 'repo')
            )

        self.assertIs(facts['in_site_packages'], False)
        self.assertIn(
            Path(sysconfig.get_paths()['purelib']).resolve(),
            [Path(root) for root in doctor_module._site_packages_paths()],
        )


if __name__ == '__main__':
    unittest.main()
