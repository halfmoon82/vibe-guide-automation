"""逐个导入 vibe_guide 下所有模块，任一失败即失败。

起因：decision_boundary 用了 Dict 却没导入，3.14 延迟解析注解不报，
3.9–3.13 上任何 vibe 命令启动即 NameError。
本测试在发布门里也用 Python 3.9（项目声明的最低版本）跑一次。
"""
import importlib
import pkgutil
import unittest
from pathlib import Path

import vibe_guide

ROOT = Path(__file__).resolve().parent.parent

# 只在特定平台才能导入的可选模块：模块名 -> 豁免原因。当前没有。
# 新增豁免必须写明原因；不得整体跳过。
PLATFORM_ONLY_EXEMPTIONS = {}


def _all_modules():
    return sorted(
        info.name
        for info in pkgutil.walk_packages(vibe_guide.__path__, "vibe_guide.")
        if not info.name.endswith(".__main__")
    )


class ModuleImportTests(unittest.TestCase):
    def test_every_module_imports(self):
        modules = _all_modules()
        self.assertGreater(len(modules), 50)
        failures = []
        for name in modules:
            if name in PLATFORM_ONLY_EXEMPTIONS:
                self.assertTrue(PLATFORM_ONLY_EXEMPTIONS[name].strip())
                continue
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001 - 要报出模块名
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "模块导入失败:\n" + "\n".join(failures))

    def test_decision_boundary_has_dict_import(self):
        from vibe_guide import decision_boundary

        self.assertTrue(hasattr(decision_boundary, "Dict"))


class ReleaseGateRunsImportTestOnMinPython(unittest.TestCase):
    """门禁自检：发布门必须在 Python 3.9 上跑本导入测试。"""

    def test_release_gate_has_py39_import_step(self):
        text = (ROOT / ".github/workflows/release-tag-gate.yml").read_text(encoding="utf-8")
        steps = text.split("\n      - name: ")
        hits = [
            st for st in steps
            if '"$LEGACY_PY" -m unittest tests.test_module_imports' in st
        ]
        self.assertTrue(hits, "发布门缺少在 Python 3.9 上运行 tests.test_module_imports 的步骤")
        self.assertIn("steps.legacy-python.outputs.python-path", hits[0])
        self.assertIn("legacy-python", text)


if __name__ == "__main__":
    unittest.main()
