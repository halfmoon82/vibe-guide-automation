"""The platform capability ledger is the only place platform capabilities live.

Acceptance (platform-ledger node):
1) DISPATCH_TOPOLOGY_MATRIX / WAKE_CHANNELS / WAKE_NO_CHANNEL_REASON /
   REMOTE_GIT_ENFORCEMENT are assigned only in the ledger module.
2) The ledger reads the same from an installed wheel as from the source tree,
   and the sdist carries it.
3) Cursor's row is unverified throughout and the card says 「未验证」.
4) WorkBuddy's new card keeps the 10-minute fallback; a run authorized under
   5.2.5 keeps its runtime parameters after the upgrade.
"""
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from vibe_guide import platform_ledger
from vibe_guide.authorization import build_authorization_card
from vibe_guide.paths import ProjectPaths
from vibe_guide.platform_ledger import (
    DISPATCH_TOPOLOGY_MATRIX,
    FIELDS,
    LEDGER_PATH,
    PLATFORMS,
    REMOTE_GIT_ENFORCEMENT,
    WAKE_CHANNELS,
    WAKE_NO_CHANNEL_REASON,
    Ledger,
    LedgerError,
    card_capability_rows,
    load_ledger,
)
from vibe_guide.publish import _default_heartbeat_minutes
from vibe_guide.state import RunSnapshot
from vibe_guide.supervisor import (
    heartbeat_interval,
    heartbeat_interval_options,
    provider_never_rotates,
    register_supervisor_address,
    wake_channel,
)

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "vibe_guide"
LEDGER_NAMES = (
    "DISPATCH_TOPOLOGY_MATRIX", "WAKE_CHANNELS", "WAKE_NO_CHANNEL_REASON",
    "REMOTE_GIT_ENFORCEMENT",
)
STATUSES = {"verified", "pm_ruling", "unverified"}


class SingleSourceTests(unittest.TestCase):
    def test_tables_are_assigned_only_in_the_ledger_module(self):
        pattern = re.compile(
            r"^\s*(?:{})\s*(?::[^=\n]*)?=(?!=)".format("|".join(LEDGER_NAMES)), re.M)
        hits = {}
        for path in sorted(PACKAGE.rglob("*.py")):
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                hits.setdefault(path.relative_to(REPO).as_posix(), []).append(match.group(0).strip())
        self.assertEqual(set(hits), {"vibe_guide/platform_ledger.py"}, hits)
        self.assertEqual(len(hits["vibe_guide/platform_ledger.py"]), len(LEDGER_NAMES))

    def test_consumers_read_the_ledger_objects(self):
        from vibe_guide import authorization, supervisor
        from vibe_guide.adapters import registry
        self.assertIs(registry.DISPATCH_TOPOLOGY_MATRIX, DISPATCH_TOPOLOGY_MATRIX)
        self.assertIs(authorization.REMOTE_GIT_ENFORCEMENT, REMOTE_GIT_ENFORCEMENT)
        self.assertEqual(set(registry.SUPPORTED_ADAPTER_IDS), set(PLATFORMS))
        self.assertFalse(hasattr(supervisor, "WAKE_CHANNELS"))
        self.assertFalse(hasattr(supervisor, "WAKE_NO_CHANNEL_REASON"))

    def test_rotation_rule_follows_the_ledger(self):
        self.assertTrue(provider_never_rotates("claude-code-background"))
        self.assertFalse(provider_never_rotates("codex-app-visible"))
        rows = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))["platforms"]
        rows["claude-code"]["rotation"] = dict(rows["claude-code"]["rotation"], value="new_thread")
        rows["codex"]["rotation"] = dict(rows["codex"]["rotation"], value="never")
        with patch.object(platform_ledger, "LEDGER", Ledger(rows)):
            self.assertFalse(provider_never_rotates("claude-code"))
            self.assertTrue(provider_never_rotates("codex"))

    def test_manifests_carry_probes_not_capability_conclusions(self):
        for path in sorted((PACKAGE / "adapters/manifests").glob("*.yaml")):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            for key in ("topology", "dispatch_topology", "wake_channel", "rotation",
                        "heartbeat", "push_block", "remote_git_enforcement"):
                self.assertNotIn(key, manifest, path.name)


class LedgerContentTests(unittest.TestCase):
    def setUp(self):
        self.ledger = load_ledger()

    def test_every_platform_has_every_cell_with_a_status(self):
        for platform in PLATFORMS:
            for field, _label in FIELDS:
                cell = self.ledger.cell(platform, field)
                self.assertIn(cell["status"], STATUSES, (platform, field))
                if cell["status"] != "unverified":
                    source = cell["source"]
                    self.assertTrue(source["date"] and source["ref"], (platform, field))
                    self.assertIn("run_id", source)
                    self.assertIn("host_version", source)

    def test_cursor_row_is_unverified_and_the_card_says_so(self):
        for field, _label in FIELDS:
            self.assertEqual(self.ledger.cell("cursor", field)["status"], "unverified", field)
        rows = card_capability_rows("cursor")
        self.assertEqual([row["field"] for row in rows], [name for name, _ in FIELDS])
        self.assertTrue(all(row["status_label"] == "未验证" for row in rows))

    def test_untested_platforms_are_unverified(self):
        for platform in ("cursor", "grok", "deepseek-harness", "kimi-code"):
            statuses = {self.ledger.cell(platform, f)["status"] for f, _ in FIELDS}
            self.assertEqual(statuses, {"unverified"}, platform)

    def test_measured_corrections(self):
        wb = self.ledger.row("workbuddy")
        self.assertIsNone(wb["worker_wake"]["value"])
        self.assertEqual(wb["supervisor_notification"]["value"], "subagent_completion")
        self.assertEqual(wb["supervisor_notification"]["status"], "verified")
        self.assertEqual(wb["heartbeat"]["value"], "automations")
        self.assertEqual(wb["nested_dispatch"]["value"], "subagent_cannot_dispatch")
        self.assertIn("SendMessage", self.ledger.cell("claude-code", "worker_wake")["note"])
        self.assertIn("reviewer", self.ledger.cell("claude-code", "worker_wake")["note"])
        # A supervisor-side notification is not a worker wake channel.
        self.assertEqual(wake_channel("workbuddy-visible")[0], None)
        self.assertNotIn("workbuddy", WAKE_CHANNELS)
        self.assertIn("workbuddy", WAKE_NO_CHANNEL_REASON)

    def test_derived_tables_keep_the_pre_ledger_values(self):
        self.assertEqual(DISPATCH_TOPOLOGY_MATRIX, {
            "codex": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
            "claude-code": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
            "cursor": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
            "kimi-code": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
            "deepseek-harness": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
            "workbuddy": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
            "grok": {"probe_pass": "dual-visible", "probe_unknown": "dual-visible"},
        })
        self.assertEqual(WAKE_CHANNELS, {"codex": "send_message_to_thread", "claude-code": "SendMessage"})
        for provider in ("claude-code", "claude-code-visible", "claude-bg", "claude-code-background"):
            self.assertEqual(wake_channel(provider), ("SendMessage", ""))
        for provider in ("codex", "codex-app-visible", "codex-background"):
            self.assertEqual(wake_channel(provider), ("send_message_to_thread", ""))


class MissingAndUnreadableTests(unittest.TestCase):
    def test_missing_platform_and_cell_read_unverified_and_conservative(self):
        ledger = Ledger({"codex": {"display_name": "Codex"}})
        for field, _ in FIELDS:
            self.assertEqual(ledger.cell("cursor", field)["status"], "unverified")
            self.assertEqual(ledger.cell("codex", field)["status"], "unverified")
        self.assertEqual(platform_ledger._topology_row(ledger, "codex"),
                         {"probe_pass": "dual-visible", "probe_unknown": "dual-visible"})
        self.assertIsNone(platform_ledger._wake_channel(ledger, "codex"))
        self.assertIn("未验证", platform_ledger._enforcement(ledger, "codex")[0]["note"])

    def test_a_trusted_cell_without_a_dated_source_is_unverified(self):
        ledger = Ledger({"codex": {"worker_wake": {"value": "send_message_to_thread",
                                                  "status": "verified", "source": {"ref": "x"}}}})
        self.assertEqual(ledger.cell("codex", "worker_wake")["status"], "unverified")
        self.assertIsNone(platform_ledger._wake_channel(ledger, "codex"))

    def test_unreadable_ledger_raises_and_publication_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "platform_ledger.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(LedgerError) as caught:
                load_ledger(bad)
            self.assertIn("unreadable", str(caught.exception))
            bad.write_text(json.dumps({"schema_version": 1, "platforms": {"nope": {}}}), encoding="utf-8")
            with self.assertRaises(LedgerError):
                load_ledger(bad)
        with patch.object(platform_ledger, "LEDGER_ERROR", "platform ledger unreadable (x): boom"):
            with self.assertRaises(ValueError) as caught:
                build_authorization_card(None, [], None)
        self.assertIn("plan publication refused", str(caught.exception))
        self.assertIn("boom", str(caught.exception))


class CardTests(unittest.TestCase):
    def _card(self, agent_id):
        from vibe_guide.models import AgentCapabilities
        sys.path.insert(0, str(Path(__file__).parent))
        from test_authorization_card_options import _plan
        plan, nodes = _plan()
        caps = AgentCapabilities(agent_id, True, True, True, False, True, "full")
        return build_authorization_card(plan, nodes, caps)

    def test_cursor_card_shows_unverified_for_every_cell(self):
        card = self._card("cursor").to_dict()
        rows = card["platform_capabilities"]
        self.assertEqual(len(rows), len(FIELDS))
        self.assertTrue(all(row["status_label"] == "未验证" for row in rows), rows)

    def test_capability_rows_are_unsigned_display(self):
        card = self._card("workbuddy")
        with patch.object(platform_ledger, "LEDGER", Ledger({})):
            bare = self._card("workbuddy")
        self.assertEqual(card.digest, bare.digest)
        labels = {row["field"]: row["status_label"] for row in card.to_dict()["platform_capabilities"]}
        self.assertEqual(labels["heartbeat"], "已验证")
        self.assertEqual(labels["integration_review"], "产品裁定")


def _workbuddy_pm_ruling_registry(root):
    """The shape of this project's real WorkBuddy registration: ruled, no channel."""
    (root / ".vibe").mkdir(exist_ok=True)
    evidence = [{"evidence_ref": "pm-ruling:2026-10-23-B", "observed_at": "2026-10-23"}]
    payload = {"platforms": {
        "workbuddy": {"channel": None, "success": evidence, "failure": evidence},
        "claude-code": {"channel": "SendMessage", "success": [{"evidence_ref": "s"}],
                        "failure": [{"evidence_ref": "f"}]},
        "codex": {"channel": "send_message_to_thread", "success": evidence, "failure": evidence},
    }}
    (root / ".vibe/push-wake-verification.json").write_text(json.dumps(payload), encoding="utf-8")


class RuntimeParameterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)
        _workbuddy_pm_ruling_registry(self.root)

    def test_workbuddy_new_card_keeps_the_ten_minute_fallback(self):
        options = heartbeat_interval_options(self.paths, "workbuddy")
        self.assertTrue(options[1].get("forced"), options)
        self.assertEqual(_default_heartbeat_minutes(options), 10)
        # A verified platform still gets the long tier by default.
        self.assertEqual(_default_heartbeat_minutes(
            heartbeat_interval_options(self.paths, "claude-code")), 60)

    def test_a_run_authorized_under_525_keeps_its_parameters(self):
        snapshot = RunSnapshot(
            "run-1", "p1", 1, "running", {"n1": {"status": "review"}}, {},
            authorization_digest="a" * 64, node_contract_digest="b" * 64,
        )
        self.addCleanup(patch.stopall)
        patch("vibe_guide.supervisor.load_snapshot", return_value=snapshot).start()
        patch("vibe_guide.adapters.task_provider.require_entry").start()
        plan_dir = self.paths.vibe / "plans/p1"
        plan_dir.mkdir(parents=True)
        # (provider, recorded choice) -> minutes under the 5.2.5 tables.
        expected = {
            ("claude-code", 60): 60, ("claude-code", 10): 10, ("claude-code", None): 60,
            ("codex-app-visible", 60): 60, ("codex-app-visible", None): 60,
            ("workbuddy-visible", 60): 10, ("workbuddy-visible", 10): 10,
            ("workbuddy-visible", None): 10,
        }
        for index, ((provider, chosen), minutes) in enumerate(expected.items()):
            run_id = "run-%d" % index
            with self.subTest(provider=provider, chosen=chosen):
                (self.paths.vibe / "runs" / run_id).mkdir(parents=True)
                register_supervisor_address(
                    self.paths, run_id, {"provider": provider, "session_id": "s", "host": "h"})
                payload = {"schema_version": 2, "plan_id": "p1"}
                if chosen is not None:
                    payload["heartbeat_interval_minutes"] = chosen
                (plan_dir / "authorization.json").write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(
                    heartbeat_interval(self.paths, run_id, decision_only=False)["minutes"], minutes)


def _build_tool_available():
    try:
        import setuptools  # noqa: F401
        import wheel  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_build_tool_available(), "setuptools/wheel not installed")
class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        work = Path(cls.tmp.name) / "src"
        work.mkdir()
        for name in ("setup.py", "pyproject.toml", "MANIFEST.in", "README.md", "README.en.md"):
            if (REPO / name).exists():
                shutil.copy2(REPO / name, work / name)
        shutil.copytree(PACKAGE, work / "vibe_guide",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cls.dist = Path(cls.tmp.name) / "dist"
        cls.dist.mkdir()
        script = (
            # build_wheel rewrites sys.argv, so read the target first.
            "import sys; out = sys.argv[1]; from setuptools import build_meta as b; "
            "b.build_wheel(out); b.build_sdist(out)"
        )
        subprocess.run([sys.executable, "-c", script, str(cls.dist)], cwd=str(work),
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_wheel_install_reads_the_same_ledger(self):
        wheel_file = next(self.dist.glob("*.whl"))
        site = Path(self.tmp.name) / "site"
        with zipfile.ZipFile(wheel_file) as archive:
            self.assertIn("vibe_guide/platform_ledger.json", archive.namelist())
            archive.extractall(site)
        probe = (
            "import json, sys; sys.path.insert(0, sys.argv[1]);"
            "from vibe_guide import platform_ledger as L;"
            "assert L.LEDGER_PATH.parent.parent == __import__('pathlib').Path(sys.argv[1]), L.LEDGER_PATH;"
            "print(json.dumps({'error': L.LEDGER_ERROR, 'rows': {p: L.LEDGER.row(p) for p in L.PLATFORMS},"
            "'matrix': L.DISPATCH_TOPOLOGY_MATRIX, 'wake': L.WAKE_CHANNELS,"
            "'enforcement': list(L.REMOTE_GIT_ENFORCEMENT)}, sort_keys=True))"
        )
        result = subprocess.run([sys.executable, "-I", "-c", probe, str(site)],
                                check=True, capture_output=True, text=True, cwd=self.tmp.name)
        installed = json.loads(result.stdout)
        source = {
            "error": platform_ledger.LEDGER_ERROR,
            "rows": {p: platform_ledger.LEDGER.row(p) for p in PLATFORMS},
            "matrix": DISPATCH_TOPOLOGY_MATRIX, "wake": WAKE_CHANNELS,
            "enforcement": list(REMOTE_GIT_ENFORCEMENT),
        }
        self.assertEqual(installed["error"], "")
        self.assertEqual(installed, json.loads(json.dumps(source, sort_keys=True)))

    def test_sdist_carries_the_ledger(self):
        sdist = next(self.dist.glob("*.tar.gz"))
        with tarfile.open(sdist) as archive:
            names = archive.getnames()
        self.assertTrue(any(name.endswith("/vibe_guide/platform_ledger.json") for name in names), names)


if __name__ == "__main__":
    unittest.main()
