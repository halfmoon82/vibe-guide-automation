"""Ratchet: module-level private atomic-write / read-JSON / digest helpers must
not grow back outside the shared foundation (``fsio`` / ``digests``).

``ALLOWED_FILES`` starts as the files owned by other nodes of the migration
DAG; ``ALLOWED_HELPERS`` lists the helpers deliberately kept in migrated files
because their output cannot be byte-identical to the shared one.  Shrink these
sets; never add to them to silence a new helper -- call ``fsio``/``digests``.
"""

import ast
import fnmatch
import re
import tempfile
import unittest
from pathlib import Path

from vibe_guide import authorize_entry, checkpoint, decisions, models, session_bypass, session_entry, skills

PACKAGE = Path(__file__).resolve().parents[1] / "vibe_guide"

_NAME = re.compile(
    r"^_(atomic\w*|write_json\w*|write_record\w*|read_json\w*|load_json\w*|\w*digest\w*|sha\d*|\w*_sha\d+\w*)$"
)

# Files owned by other nodes of the helper-migration DAG (they migrate them).
# fnmatch patterns, matched against the path relative to vibe_guide/.
ALLOWED_FILES = (
    "adapters/claude_bg.py",
    "adapters/task_provider.py",
    "authorization.py",
    "binding_contract.py",
    "binding_lifecycle.py",
    "capability_contract.py",
    "change_requests.py",
    "cli.py",
    "engine_attestation.py",
    "history_migration.py",
    "host_rules.py",
    "initializer.py",
    "manifest.py",
    "m*_servers/workbuddy_jobs.py",
    "migration.py",
    "runners/local_worker.py",
    "state.py",
    "supervisor.py",
    "task_registry.py",
)

# (file pattern, helper) kept in migrated files: the output cannot be
# byte-identical to the shared helper, or the helper adds its own contract.
ALLOWED_HELPERS = frozenset(
    {
        # indent=2 / trailing-newline layouts differ from fsio's compact bytes
        ("authorize_entry.py", "_atomic_json"),
        ("installation.py", "_atomic_json"),
        ("session_entry.py", "materialize_session_entry"),
        ("diagnostics.py", "screen_session"),
        ("skills.py", "_write_record_atomic"),
        ("m*_servers/workbuddy_sessions.py", "_write_json"),
        # reads with their own size bound / type check / error contract
        ("authorize_entry.py", "_read_json"),
        ("decisions.py", "_read_json"),
        ("installation.py", "_read_json"),
        ("runners/local.py", "_read_json"),
        ("m*_servers/workbuddy_sessions.py", "_load_json"),
        # size bound before delegating to fsio.atomic_write_bytes
        ("decisions.py", "_atomic_json"),
        ("runners/local.py", "_atomic_json"),
        ("session_bypass.py", "_write_json"),
        # previous-copy rotation around the replace
        ("checkpoint.py", "write_checkpoint"),
        # digest semantics differ (ensure_ascii / default=str / tree walk)
        ("runners/local.py", "_digest"),
        ("stall.py", "_digest"),
        ("skills.py", "_materialized_tree_sha256"),
        # named wrapper over digests.sha256_bytes
        ("session_bypass.py", "_digest"),
        ("authorize_entry.py", "_sha"),  # tests/test_digests.py pins it against digests.sha256_file
    }
)


def _helper_names(source):
    tree = ast.parse(source)
    found = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _NAME.match(node.name):
            found.append(node.name)
            continue
        calls = {
            ast.unparse(child.func)
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
        }
        if "os.replace" in calls and "tempfile.mkstemp" in calls:
            found.append(node.name)
    return found


def _matches(relative, patterns):
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _violations(root):
    result = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in ("fsio.py", "digests.py") or _matches(relative, ALLOWED_FILES):
            continue
        for name in _helper_names(path.read_text(encoding="utf-8")):
            if not any(
                fnmatch.fnmatch(relative, pattern) and name == helper
                for pattern, helper in ALLOWED_HELPERS
            ):
                result.append("%s:%s" % (relative, name))
    return result


class HelperRatchetTest(unittest.TestCase):
    def test_no_new_private_helpers_outside_allowlist(self):
        self.assertEqual(_violations(PACKAGE), [])

    def test_new_private_atomic_helper_fails_the_ratchet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fresh.py").write_text("def _atomic_json(path, payload):\n    pass\n")
            self.assertEqual(_violations(root), ["fresh.py:_atomic_json"])

    def test_renamed_atomic_helper_is_caught_by_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fresh.py").write_text(
                "import os, tempfile\n"
                "def save(path):\n"
                "    name = tempfile.mkstemp()\n"
                "    os.replace(name, path)\n"
            )
            self.assertEqual(_violations(root), ["fresh.py:save"])

    def test_allowlist_entries_are_not_stale(self):
        files = [p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*.py")]
        for pattern in ALLOWED_FILES:
            self.assertTrue(any(fnmatch.fnmatch(f, pattern) for f in files), pattern)
        for pattern, helper in ALLOWED_HELPERS:
            hits = [
                f for f in files
                if fnmatch.fnmatch(f, pattern)
                and helper in _helper_names((PACKAGE / f).read_text(encoding="utf-8"))
            ]
            self.assertTrue(hits, "stale allowlist entry %s:%s" % (pattern, helper))

    def test_removed_module_stays_removed(self):
        self.assertFalse((PACKAGE / "capability_wizard.py").exists())
        for path in PACKAGE.rglob("*.py"):
            self.assertNotIn("capability_wizard", path.read_text(encoding="utf-8"), str(path))


# Golden values recorded before migration: migrated callers keep their bytes.
class MigratedDigestGoldenTest(unittest.TestCase):
    def test_decisions_canonical_digest(self):
        self.assertEqual(
            decisions.canonical_digest({"a": "中", "b": [1, 2]}),
            "2b399f10d6e1c87064ef336b1ad1dcda1a4ae60be606f431d65460880cad309b",
        )

    def test_session_bypass_digest(self):
        self.assertEqual(
            session_bypass._digest("挑战"),
            "4eed57b5a12b5dc42bf4b315151be1603b5995e8eb88fcbb18d1af0b0d04c930",
        )

    def test_authorize_entry_file_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "f"
            path.write_bytes(b"hello" * 300000)
            self.assertEqual(
                authorize_entry._sha(path),
                "796374667fc0edbeb8a1455bdef32ba3e48763e21aacc2660ceb12787d1910f0",
            )

    def test_session_entry_plan_id(self):
        self.assertEqual(session_entry.stable_plan_id("Fix  The Bug 中"), "session-0af7ef0a1c708369")

    def test_node_worktree_suffix(self):
        self.assertEqual(models.node_worktree("a."), ".worktrees/a-5ab640fa")
        self.assertEqual(models.node_worktree("a"), ".worktrees/a-ca978112")

    def test_vendor_dir(self):
        self.assertEqual(
            str(skills.repository_vendor_dir(Path("/v"), "https://github.com/o/r")),
            "/v/vendor/o-r-97393e7e6b5ab8df",
        )

    def test_target_contract_digest(self):
        contract = models.TargetContract(provider="github", repository="中/r", file_scope=("a", "b"))
        self.assertEqual(
            contract.digest,
            "f765b8adaa59886a9207882c97e564c35f5fafc790d87e916e0ff83e3ad4c662",
        )

    def test_checkpoint_sha(self):
        value = checkpoint.MonitorCheckpoint("run-1", "plan-1@1", 1, 1, "poll", ["停"])
        self.assertEqual(
            value.sha256,
            "c16fd0fa2545b625434c9dda8aed2fb64be480e093753d48552332147842601a",
        )


class MigratedWriterGoldenTest(unittest.TestCase):
    def test_decision_record_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "d.json"
            decisions._atomic_json(path, {"b": "中", "a": 1})
            self.assertEqual(path.read_bytes(), '{"a":1,"b":"中"}\n'.encode("utf-8"))

    def test_bypass_record_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b.json"
            session_bypass._write_json(path, {"b": "中", "a": 1})
            self.assertEqual(path.read_bytes(), '{"a":1,"b":"中"}\n'.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
