"""bg-wiring: Claude Code "background launch verified" capability fact.

Registering ``claude-code.background_task.launch`` as true (with provenance)
reports the adapter as a degraded background provider, routes dispatch to the
background topology and makes the authorization card disclose the three
downgrade limitations.  Not registering it (or registering it false / without
provenance) must leave every report byte-for-byte what it was before the fact
existed -- the GOLDEN_* literals below were captured from the unmodified code.
"""
import copy
import importlib
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import authorization
from vibe_guide.adapters.base import (
    BACKGROUND_LAUNCH_LIMITATIONS,
    TOPOLOGY_BACKGROUND,
    Environment,
    ManifestAdapter,
    ManifestError,
)
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.adapters.task_provider import ProviderUnavailable
from vibe_guide.attest import optional_fact_names, record_session_capabilities, validate_session_facts
from vibe_guide.authorization import build_authorization_card, validate_authorization_card_consistency
from vibe_guide.cli import (
    _background_launcher_problem,
    _background_worker_entries,
    _card_discloses_background,
    _observed_topology_rulings,
    _public_runner,
    run_cli,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import ProviderActionRunner

P = "claude-code."
BG = P + "background_task.launch"
LAUNCHER_MODULE = "vibe_guide.adapters.claude_bg"
RUNNER_MODULE = "vibe_guide.runners.claude_bg"


# Baseline captured from the unmodified code (before bg-wiring) via capability_report(env, 'p1'),
# record_session_capabilities and _observed_topology_rulings.
GOLDEN_REPORTS = {'a': {'adapter_id': 'claude-code',
       'authorization_card': None,
       'capabilities': {'agent_id': 'claude-code',
                        'background': False,
                        'create_task': False,
                        'direct_enter': False,
                        'enter_task': False,
                        'evidence': {'claude-code.agent': True,
                                     'claude-code.in_session_sdd': False,
                                     'claude-code.shell': False,
                                     'claude-code.subprocess': False,
                                     'claude-code.visible_task.create': False,
                                     'claude-code.visible_task.enter': False,
                                     'claude-code.visible_task.resume': False,
                                     'claude-code.visible_task.wait': False,
                                     'claude-code.worktree': False},
                        'in_session_sdd': False,
                        'level': 'guide',
                        'limitations': ['无法启动 subprocess；仅保留向导能力'],
                        'mode': 'guide',
                        'provenance': {'claude-code.agent': None,
                                       'claude-code.in_session_sdd': None,
                                       'claude-code.shell': None,
                                       'claude-code.subprocess': None,
                                       'claude-code.visible_task.create': None,
                                       'claude-code.visible_task.enter': None,
                                       'claude-code.visible_task.resume': None,
                                       'claude-code.visible_task.wait': None,
                                       'claude-code.worktree': None},
                        'provider': '',
                        'resume_task': False,
                        'session_resume': False,
                        'shell': False,
                        'subprocess': False,
                        'visible_automation': False,
                        'wait_task': False,
                        'worktree': False},
       'capability_level': 'guide',
       'detected': True,
       'evidence': {'claude-code.agent': True,
                    'claude-code.in_session_sdd': False,
                    'claude-code.shell': False,
                    'claude-code.subprocess': False,
                    'claude-code.visible_task.create': False,
                    'claude-code.visible_task.enter': False,
                    'claude-code.visible_task.resume': False,
                    'claude-code.visible_task.wait': False,
                    'claude-code.worktree': False},
       'mode': 'guide',
       'monitor_command': ['vibe', 'monitor', '--plan', 'p1', '--json'],
       'provider': '',
       'reason': None,
       'topology': {'adapter_id': 'claude-code',
                    'evidence_ref': None,
                    'probe': 'claude-code.in_session_sdd',
                    'probe_status': 'unknown',
                    'topology': 'dual-visible',
                    'upgraded': False}},
 'b': {'adapter_id': 'claude-code',
       'authorization_card': None,
       'capabilities': {'agent_id': 'claude-code',
                        'background': False,
                        'create_task': False,
                        'direct_enter': False,
                        'enter_task': False,
                        'evidence': {'claude-code.agent': True,
                                     'claude-code.in_session_sdd': False,
                                     'claude-code.shell': True,
                                     'claude-code.subprocess': True,
                                     'claude-code.visible_task.create': False,
                                     'claude-code.visible_task.enter': False,
                                     'claude-code.visible_task.resume': False,
                                     'claude-code.visible_task.wait': False,
                                     'claude-code.worktree': True},
                        'in_session_sdd': False,
                        'level': 'guide',
                        'limitations': ['未验证原生桌面控制面'],
                        'mode': 'guide',
                        'provenance': {'claude-code.agent': None,
                                       'claude-code.in_session_sdd': None,
                                       'claude-code.shell': None,
                                       'claude-code.subprocess': None,
                                       'claude-code.visible_task.create': None,
                                       'claude-code.visible_task.enter': None,
                                       'claude-code.visible_task.resume': None,
                                       'claude-code.visible_task.wait': None,
                                       'claude-code.worktree': None},
                        'provider': '',
                        'resume_task': False,
                        'session_resume': False,
                        'shell': True,
                        'subprocess': True,
                        'visible_automation': False,
                        'wait_task': False,
                        'worktree': True},
       'capability_level': 'guide',
       'detected': True,
       'evidence': {'claude-code.agent': True,
                    'claude-code.in_session_sdd': False,
                    'claude-code.shell': True,
                    'claude-code.subprocess': True,
                    'claude-code.visible_task.create': False,
                    'claude-code.visible_task.enter': False,
                    'claude-code.visible_task.resume': False,
                    'claude-code.visible_task.wait': False,
                    'claude-code.worktree': True},
       'mode': 'guide',
       'monitor_command': ['vibe', 'monitor', '--plan', 'p1', '--json'],
       'provider': '',
       'reason': None,
       'topology': {'adapter_id': 'claude-code',
                    'evidence_ref': None,
                    'probe': 'claude-code.in_session_sdd',
                    'probe_status': 'unknown',
                    'topology': 'dual-visible',
                    'upgraded': False}},
 'c': {'adapter_id': 'claude-code',
       'authorization_card': None,
       'capabilities': {'agent_id': 'claude-code',
                        'background': False,
                        'create_task': False,
                        'direct_enter': False,
                        'enter_task': False,
                        'evidence': {'claude-code.agent': True,
                                     'claude-code.in_session_sdd': True,
                                     'claude-code.shell': True,
                                     'claude-code.subprocess': True,
                                     'claude-code.visible_task.create': False,
                                     'claude-code.visible_task.enter': False,
                                     'claude-code.visible_task.resume': False,
                                     'claude-code.visible_task.wait': False,
                                     'claude-code.worktree': True},
                        'in_session_sdd': True,
                        'level': 'full',
                        'limitations': ['可派发会话内 SDD 子代理（无可见任务窗口自动化）'],
                        'mode': 'in_session_sdd',
                        'provenance': {'claude-code.agent': None,
                                       'claude-code.in_session_sdd': 'probe-x',
                                       'claude-code.shell': None,
                                       'claude-code.subprocess': None,
                                       'claude-code.visible_task.create': None,
                                       'claude-code.visible_task.enter': None,
                                       'claude-code.visible_task.resume': None,
                                       'claude-code.visible_task.wait': None,
                                       'claude-code.worktree': None},
                        'provider': 'claude-code-visible',
                        'resume_task': False,
                        'session_resume': False,
                        'shell': True,
                        'subprocess': True,
                        'visible_automation': False,
                        'wait_task': False,
                        'worktree': True},
       'capability_level': 'full',
       'detected': True,
       'evidence': {'claude-code.agent': True,
                    'claude-code.in_session_sdd': True,
                    'claude-code.shell': True,
                    'claude-code.subprocess': True,
                    'claude-code.visible_task.create': False,
                    'claude-code.visible_task.enter': False,
                    'claude-code.visible_task.resume': False,
                    'claude-code.visible_task.wait': False,
                    'claude-code.worktree': True},
       'mode': 'in_session_sdd',
       'monitor_command': ['vibe', 'monitor', '--plan', 'p1', '--json'],
       'provider': 'claude-code-visible',
       'reason': None,
       'topology': {'adapter_id': 'claude-code',
                    'evidence_ref': 'probe-x',
                    'probe': 'claude-code.in_session_sdd',
                    'probe_status': 'pass',
                    'topology': 'in_session_sdd',
                    'upgraded': True}},
 'd': {'adapter_id': 'claude-code',
       'authorization_card': None,
       'capabilities': {'agent_id': 'claude-code',
                        'background': False,
                        'create_task': True,
                        'direct_enter': True,
                        'enter_task': True,
                        'evidence': {'claude-code.agent': True,
                                     'claude-code.in_session_sdd': False,
                                     'claude-code.shell': True,
                                     'claude-code.subprocess': True,
                                     'claude-code.visible_task.create': True,
                                     'claude-code.visible_task.enter': True,
                                     'claude-code.visible_task.resume': True,
                                     'claude-code.visible_task.wait': True,
                                     'claude-code.worktree': True},
                        'in_session_sdd': False,
                        'level': 'full',
                        'limitations': [],
                        'mode': 'visible',
                        'provenance': {'claude-code.agent': None,
                                       'claude-code.in_session_sdd': None,
                                       'claude-code.shell': None,
                                       'claude-code.subprocess': None,
                                       'claude-code.visible_task.create': None,
                                       'claude-code.visible_task.enter': None,
                                       'claude-code.visible_task.resume': None,
                                       'claude-code.visible_task.wait': None,
                                       'claude-code.worktree': None},
                        'provider': 'claude-code-visible',
                        'resume_task': True,
                        'session_resume': True,
                        'shell': True,
                        'subprocess': True,
                        'visible_automation': True,
                        'wait_task': True,
                        'worktree': True},
       'capability_level': 'full',
       'detected': True,
       'evidence': {'claude-code.agent': True,
                    'claude-code.in_session_sdd': False,
                    'claude-code.shell': True,
                    'claude-code.subprocess': True,
                    'claude-code.visible_task.create': True,
                    'claude-code.visible_task.enter': True,
                    'claude-code.visible_task.resume': True,
                    'claude-code.visible_task.wait': True,
                    'claude-code.worktree': True},
       'mode': 'visible',
       'monitor_command': ['vibe', 'monitor', '--plan', 'p1', '--json'],
       'provider': 'claude-code-visible',
       'reason': None,
       'topology': {'adapter_id': 'claude-code',
                    'evidence_ref': None,
                    'probe': 'claude-code.in_session_sdd',
                    'probe_status': 'unknown',
                    'topology': 'dual-visible',
                    'upgraded': False}}}

GOLDEN_ATTEST = {'a': {'adapter_id': 'claude-code',
       'capabilities_path': '.vibe/provider-actions/capabilities.json',
       'facts': {'claude-code.agent': True,
                 'claude-code.in_session_sdd': False,
                 'claude-code.shell': False,
                 'claude-code.subprocess': False,
                 'claude-code.visible_task.create': False,
                 'claude-code.visible_task.enter': False,
                 'claude-code.visible_task.resume': False,
                 'claude-code.visible_task.wait': False,
                 'claude-code.worktree': False},
       'in_session_sdd': False,
       'level': 'guide',
       'mode': 'guide',
       'project_id': 'proj',
       'visible_automation': False},
 'b': {'adapter_id': 'claude-code',
       'capabilities_path': '.vibe/provider-actions/capabilities.json',
       'facts': {'claude-code.agent': True,
                 'claude-code.in_session_sdd': False,
                 'claude-code.shell': True,
                 'claude-code.subprocess': True,
                 'claude-code.visible_task.create': False,
                 'claude-code.visible_task.enter': False,
                 'claude-code.visible_task.resume': False,
                 'claude-code.visible_task.wait': False,
                 'claude-code.worktree': True},
       'in_session_sdd': False,
       'level': 'guide',
       'mode': 'guide',
       'project_id': 'proj',
       'visible_automation': False},
 'c': {'adapter_id': 'claude-code',
       'capabilities_path': '.vibe/provider-actions/capabilities.json',
       'facts': {'claude-code.agent': True,
                 'claude-code.in_session_sdd': True,
                 'claude-code.shell': True,
                 'claude-code.subprocess': True,
                 'claude-code.visible_task.create': False,
                 'claude-code.visible_task.enter': False,
                 'claude-code.visible_task.resume': False,
                 'claude-code.visible_task.wait': False,
                 'claude-code.worktree': True},
       'in_session_sdd': True,
       'level': 'full',
       'mode': 'in_session_sdd',
       'project_id': 'proj',
       'visible_automation': False},
 'd': {'adapter_id': 'claude-code',
       'capabilities_path': '.vibe/provider-actions/capabilities.json',
       'facts': {'claude-code.agent': True,
                 'claude-code.in_session_sdd': False,
                 'claude-code.shell': True,
                 'claude-code.subprocess': True,
                 'claude-code.visible_task.create': True,
                 'claude-code.visible_task.enter': True,
                 'claude-code.visible_task.resume': True,
                 'claude-code.visible_task.wait': True,
                 'claude-code.worktree': True},
       'in_session_sdd': False,
       'level': 'full',
       'mode': 'visible',
       'project_id': 'proj',
       'visible_automation': True}}

GOLDEN_RULINGS = {'a': {'claude-code': 'dual-visible'},
 'b': {'claude-code': 'dual-visible'},
 'c': {'claude-code': 'in_session_sdd'},
 'd': {'claude-code': 'dual-visible'}}


def env_a():
    return Environment(commands={P + "agent": True}, facts={}, available_agents=("claude-code",))


def env_b():
    return Environment(
        commands={P + "agent": True},
        facts={P + "shell": True, P + "subprocess": True, P + "worktree": True},
        available_agents=("claude-code",),
    )


def env_c():
    return Environment(
        commands={P + "agent": True},
        facts={P + "shell": True, P + "subprocess": True, P + "worktree": True, P + "in_session_sdd": True},
        provenance={P + "in_session_sdd": "probe-x"},
        available_agents=("claude-code",),
    )


def env_d():
    facts = {P + "shell": True, P + "subprocess": True, P + "worktree": True}
    for suffix in ("create", "enter", "resume", "wait"):
        facts[P + "visible_task." + suffix] = True
    return Environment(commands={P + "agent": True}, facts=facts, available_agents=("claude-code",))


ENVS = {"a": env_a, "b": env_b, "c": env_c, "d": env_d}


def with_bg(env, value=True, provenance="probe-bg"):
    facts = dict(env.facts)
    facts[BG] = value
    prov = dict(env.provenance)
    if provenance:
        prov[BG] = provenance
    return Environment(
        commands=env.commands, facts=facts, provenance=prov, available_agents=env.available_agents
    )


def adapter():
    return AdapterRegistry().get("claude-code")


class BaselineRegressionTests(unittest.TestCase):
    """Not registering the background fact leaves everything as it was."""

    def test_capability_report_is_unchanged_without_the_fact(self):
        for key, make in ENVS.items():
            with self.subTest(env=key):
                report = adapter().capability_report(make(), "p1")
                self.assertEqual(report, GOLDEN_REPORTS[key])

    def test_attest_summary_and_rulings_are_unchanged_without_the_fact(self):
        base = {
            P + "agent": True, P + "shell": False, P + "subprocess": False, P + "worktree": False,
            P + "visible_task.create": False, P + "visible_task.enter": False,
            P + "visible_task.resume": False, P + "visible_task.wait": False,
            P + "in_session_sdd": False,
        }
        three = {P + "shell": True, P + "subprocess": True, P + "worktree": True}
        visible = {P + "visible_task." + s: True for s in ("create", "enter", "resume", "wait")}
        cases = {
            "a": dict(base),
            "b": {**base, **three},
            "c": {**base, **three, P + "in_session_sdd": True},
            "d": {**base, **three, **visible},
        }
        for key, facts in cases.items():
            with self.subTest(env=key):
                root = Path(tempfile.mkdtemp(prefix="bgw-base-"))
                self.addCleanup(shutil.rmtree, root, True)
                self.assertEqual(run_cli(["init", "--confirm", "--json"], root).payload.get("status"), "ok")
                paths = ProjectPaths(root)
                out = record_session_capabilities(paths, "claude-code", facts, "prov-1", "proj")
                self.assertEqual(out, GOLDEN_ATTEST[key])
                self.assertEqual(_observed_topology_rulings(paths), GOLDEN_RULINGS[key])


class BackgroundCapabilityTests(unittest.TestCase):
    def test_registered_true_reports_degraded_background(self):
        report = adapter().capability_report(with_bg(env_b()), "p1")
        caps = report["capabilities"]
        self.assertEqual(report["mode"], "background")
        self.assertEqual(report["provider"], "claude-code-background")
        self.assertEqual(report["capability_level"], "background")
        self.assertEqual(caps["level"], "background")
        self.assertIs(caps["background"], True)
        self.assertEqual(caps["limitations"], list(BACKGROUND_LAUNCH_LIMITATIONS))
        self.assertEqual(
            report["topology"],
            {"adapter_id": "claude-code", "probe": BG, "probe_status": "pass",
             "topology": "background", "evidence_ref": "probe-bg", "upgraded": True},
        )
        self.assertIs(report["evidence"][BG], True)
        self.assertEqual(caps["provenance"][BG], "probe-bg")

    def test_background_wins_over_visible_and_sdd_but_keeps_their_booleans(self):
        report = adapter().capability_report(with_bg(env_d()), "p1")
        self.assertEqual(report["mode"], "background")
        self.assertIs(report["capabilities"]["visible_automation"], True)
        sdd = adapter().capability_report(with_bg(env_c()), "p1")
        self.assertEqual(sdd["mode"], "background")
        self.assertIs(sdd["capabilities"]["in_session_sdd"], True)

    def test_background_requires_subprocess(self):
        report = adapter().capability_report(with_bg(env_a()), "p1")
        self.assertEqual(report["mode"], "guide")
        self.assertEqual(report["provider"], "")

    def test_false_or_missing_provenance_does_not_upgrade(self):
        for label, env in (
            ("false", with_bg(env_b(), value=False)),
            ("no-provenance", with_bg(env_b(), provenance=None)),
        ):
            with self.subTest(case=label):
                report = adapter().capability_report(env, "p1")
                golden = GOLDEN_REPORTS["b"]
                for key in ("mode", "provider", "capability_level", "topology"):
                    self.assertEqual(report[key], golden[key])
                self.assertEqual(report["capabilities"]["limitations"], golden["capabilities"]["limitations"])
                self.assertEqual(report["capabilities"]["level"], golden["capabilities"]["level"])
                # Only the extra stated fact shows up; nothing else moves.
                evidence = dict(report["evidence"])
                evidence.pop(BG)
                self.assertEqual(evidence, golden["evidence"])

    def test_background_launch_status_values(self):
        a = adapter()
        self.assertEqual(a.background_launch_status(env_b()), ("unknown", None))
        self.assertEqual(a.background_launch_status(with_bg(env_b())), ("pass", "probe-bg"))
        self.assertEqual(a.background_launch_status(with_bg(env_b(), provenance=None)), ("unknown", None))
        self.assertEqual(a.background_launch_status(with_bg(env_b(), value=False)), ("unsupported", "probe-bg"))
        codex = AdapterRegistry().get("codex")
        self.assertEqual(codex.background_launch_status(env_b()), ("unsupported", None))

    def test_limitations_satisfy_the_authorization_disclosure_check(self):
        self.assertEqual(
            authorization._background_limitation_gaps(tuple(BACKGROUND_LAUNCH_LIMITATIONS)), []
        )
        self.assertEqual(TOPOLOGY_BACKGROUND, "background")

    def test_provider_for_background_uses_background_provider(self):
        a = adapter()
        caps = a.detect(with_bg(env_b())).capabilities
        provider = a.provider_for(caps)
        self.assertEqual(provider.provider, "claude-code-background")

    def test_manifest_declares_background_probe_outside_probes(self):
        manifest = adapter().manifest
        self.assertEqual(manifest["background_probe"], BG)
        self.assertNotIn(BG, [p["name"] for p in manifest["probes"]])

    def test_other_adapters_have_no_background_probe(self):
        for adapter_id in ("codex", "cursor", "grok", "workbuddy", "kimi-code", "deepseek-harness"):
            self.assertNotIn("background_probe", AdapterRegistry().get(adapter_id).manifest)

    def test_manifest_background_probe_validation(self):
        base = copy.deepcopy(adapter().manifest)
        for bad in ("", 5, "other.background_task.launch", "claude-code.agent"):
            with self.subTest(value=bad):
                manifest = dict(base, background_probe=bad)
                with self.assertRaises(ManifestError):
                    ManifestAdapter.validate_manifest(manifest)
        ok = ManifestAdapter.validate_manifest(dict(base))
        self.assertEqual(ok["background_probe"], BG)


class AttestOptionalFactTests(unittest.TestCase):
    def facts(self, **extra):
        facts = {
            P + "agent": True, P + "shell": True, P + "subprocess": True, P + "worktree": True,
            P + "visible_task.create": False, P + "visible_task.enter": False,
            P + "visible_task.resume": False, P + "visible_task.wait": False,
            P + "in_session_sdd": False,
        }
        facts.update(extra)
        return facts

    def test_optional_fact_names(self):
        self.assertEqual(optional_fact_names("claude-code"), [BG])
        self.assertEqual(optional_fact_names("cursor"), [])

    def test_optional_fact_accepted_and_returned_after_probes(self):
        validated = validate_session_facts("claude-code", self.facts(**{BG: True}))
        self.assertIs(validated[BG], True)
        self.assertEqual(list(validated)[-1], BG)

    def test_optional_fact_may_be_omitted(self):
        self.assertNotIn(BG, validate_session_facts("claude-code", self.facts()))

    def test_optional_fact_must_be_bool(self):
        with self.assertRaises(ValueError):
            validate_session_facts("claude-code", self.facts(**{BG: "yes"}))

    def test_probes_remain_required(self):
        facts = self.facts(**{BG: True})
        del facts[P + "worktree"]
        with self.assertRaises(ValueError):
            validate_session_facts("claude-code", facts)

    def test_other_adapters_reject_the_name(self):
        with self.assertRaises(ValueError):
            validate_session_facts("cursor", {"cursor.background_task.launch": True})
        with self.assertRaises(ValueError):
            validate_session_facts("claude-code", self.facts(**{"cursor.background_task.launch": True}))

    def test_attest_summary_reports_background_mode(self):
        root = Path(tempfile.mkdtemp(prefix="bgw-att-"))
        self.addCleanup(shutil.rmtree, root, True)
        run_cli(["init", "--confirm", "--json"], root)
        out = record_session_capabilities(
            ProjectPaths(root), "claude-code", self.facts(**{BG: True}), "prov", "proj"
        )
        self.assertEqual(out["mode"], "background")
        self.assertEqual(out["level"], "background")
        self.assertIs(out["facts"][BG], True)

    def test_attest_cli_prints_background_dispatch_note(self):
        root = Path(tempfile.mkdtemp(prefix="bgw-attcli-"))
        self.addCleanup(shutil.rmtree, root, True)
        run_cli(["init", "--confirm", "--json"], root)
        (root / "facts.json").write_text(json.dumps(self.facts(**{BG: True})), encoding="utf-8")
        result = run_cli(
            ["attest", "--adapter", "claude-code", "--facts", "facts.json", "--provenance", "p"], root
        )
        self.assertIn("可派发降级后台会话（桌面不可见，只能终端 attach 进入）", result.text)


def make_node(node_id, files, adapter_id="claude-code"):
    return DAGNode(
        node_id, node_id, [], [], "parallel",
        {"files": files, "worker": "worker-" + node_id, "worktree": ".worktrees/" + node_id,
         "adapter_id": adapter_id},
        "ready",
    )


def make_card(rulings=None, workers=None):
    nodes = [make_node("n1", ["a.py"]), make_node("n2", ["b.py"])]
    plan = Plan("plan-1", 1, "docs/prd.md", ["n1", "n2"], "draft")
    caps = AgentCapabilities("claude-code", True, True, True, True, True, "full")
    kwargs = {}
    if rulings is not None:
        kwargs = {"topology_rulings": rulings, "workers": workers}
    return plan, nodes, build_authorization_card(plan, nodes, caps, **kwargs)


def fake_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def missing_launcher():
    return mock.patch.dict(sys.modules, {LAUNCHER_MODULE: None})


class ProjectCase(unittest.TestCase):
    def project(self, facts_extra):
        root = Path(tempfile.mkdtemp(prefix="bgw-proj-"))
        self.addCleanup(shutil.rmtree, root, True)
        run_cli(["init", "--confirm", "--json"], root)
        facts = {
            P + "agent": True, P + "shell": True, P + "subprocess": True, P + "worktree": True,
            P + "visible_task.create": False, P + "visible_task.enter": False,
            P + "visible_task.resume": False, P + "visible_task.wait": False,
            P + "in_session_sdd": False,
        }
        facts.update(facts_extra)
        paths = ProjectPaths(root)
        record_session_capabilities(paths, "claude-code", facts, "prov", "proj")
        return paths


class TopologyRulingTests(ProjectCase):
    def test_registered_fact_rules_background(self):
        paths = self.project({BG: True})
        self.assertEqual(_observed_topology_rulings(paths), {"claude-code": "background"})

    def test_false_fact_keeps_original_ruling(self):
        paths = self.project({BG: False})
        self.assertEqual(_observed_topology_rulings(paths), {"claude-code": "dual-visible"})

    def test_card_without_background_workers_falls_back(self):
        paths = self.project({BG: True})
        _plan, _nodes, old_card = make_card()
        self.assertFalse(_card_discloses_background(old_card))
        self.assertEqual(
            _observed_topology_rulings(paths, old_card), {"claude-code": "dual-visible"}
        )

    def test_fallback_keeps_sdd_ruling_when_sdd_fact_passes(self):
        paths = self.project({BG: True, P + "in_session_sdd": True})
        _plan, _nodes, old_card = make_card()
        self.assertEqual(
            _observed_topology_rulings(paths, old_card), {"claude-code": "in_session_sdd"}
        )

    def test_card_with_background_workers_stays_background(self):
        paths = self.project({BG: True})
        rulings = {"claude-code": "background"}
        with missing_launcher():
            _plan, nodes, _ = make_card()
            entries = _background_worker_entries(nodes, rulings)
        _plan, _nodes, bg_card = make_card(rulings, entries)
        self.assertTrue(_card_discloses_background(bg_card))
        self.assertEqual(_observed_topology_rulings(paths, bg_card), rulings)

    def test_card_none_and_empty_do_not_disclose(self):
        self.assertFalse(_card_discloses_background(None))


class AuthorizationCardTests(unittest.TestCase):
    RULINGS = {"claude-code": "background"}

    def test_no_background_nodes_returns_none(self):
        nodes = [make_node("n1", ["a.py"])]
        self.assertIsNone(_background_worker_entries(nodes, {"claude-code": "dual-visible"}))
        self.assertIsNone(_background_worker_entries(nodes, {}))

    def test_only_background_ruled_nodes_get_entries(self):
        nodes = [make_node("n1", ["a.py"]), make_node("n2", ["b.py"], adapter_id="codex")]
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: fake_module(
                LAUNCHER_MODULE, configuration_problem=lambda: None)}):
            entries = _background_worker_entries(nodes, self.RULINGS)
        self.assertEqual(list(entries), ["n1"])
        self.assertEqual(entries["n1"]["topology"], "background")
        self.assertEqual(entries["n1"]["mode"], "background")
        self.assertEqual(entries["n1"]["limitations"], list(BACKGROUND_LAUNCH_LIMITATIONS))

    def test_first_card_discloses_three_limitations(self):
        nodes = [make_node("n1", ["a.py"]), make_node("n2", ["b.py"])]
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: fake_module(
                LAUNCHER_MODULE, configuration_problem=lambda: None)}):
            entries = _background_worker_entries(nodes, self.RULINGS)
        _plan, _nodes, card = make_card(self.RULINGS, entries)
        self.assertEqual(card.topology_summary["background"], 2)
        self.assertEqual(card.topology_summary["by_topology"]["background"], 2)
        for worker in card.workers:
            self.assertEqual(worker["topology"], "background")
            self.assertEqual(worker["mode"], "background")
            self.assertEqual(len(worker["limitations"]), 3)
            self.assertEqual(authorization._background_limitation_gaps(worker["limitations"]), [])
        self.assertTrue(validate_authorization_card_consistency(card))
        self.assertTrue(_card_discloses_background(card))

    def test_missing_launcher_is_reported_in_limitations(self):
        nodes = [make_node("n1", ["a.py"])]
        with missing_launcher():
            entries = _background_worker_entries(nodes, self.RULINGS)
        limitations = entries["n1"]["limitations"]
        self.assertEqual(limitations[:3], list(BACKGROUND_LAUNCH_LIMITATIONS))
        self.assertEqual(len(limitations), 4)
        self.assertTrue(limitations[3].startswith("后台启动器不可用："))
        self.assertIn("后台启动器未安装", limitations[3])

    def test_launcher_configuration_problem_string_is_reported(self):
        nodes = [make_node("n1", ["a.py"])]
        module = fake_module(LAUNCHER_MODULE, configuration_problem=lambda: "缺少 claude 可执行文件")
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: module}):
            entries = _background_worker_entries(nodes, self.RULINGS)
        self.assertEqual(entries["n1"]["limitations"][3], "后台启动器不可用：缺少 claude 可执行文件")

    def test_background_card_survives_validation_with_unavailable_launcher(self):
        nodes = [make_node("n1", ["a.py"]), make_node("n2", ["b.py"])]
        with missing_launcher():
            entries = _background_worker_entries(nodes, self.RULINGS)
        _plan, _nodes, card = make_card(self.RULINGS, entries)
        self.assertTrue(validate_authorization_card_consistency(card))


class LauncherProblemTests(unittest.TestCase):
    def test_module_missing(self):
        with missing_launcher():
            self.assertEqual(
                _background_launcher_problem(), "后台启动器未安装（缺少 vibe_guide.adapters.claude_bg）"
            )

    def test_configuration_problem_none_means_ok(self):
        module = fake_module(LAUNCHER_MODULE, configuration_problem=lambda: None)
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: module}):
            self.assertIsNone(_background_launcher_problem())

    def test_configuration_problem_empty_string_means_ok(self):
        module = fake_module(LAUNCHER_MODULE, configuration_problem=lambda: "")
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: module}):
            self.assertIsNone(_background_launcher_problem())

    def test_configuration_problem_string_is_returned(self):
        module = fake_module(LAUNCHER_MODULE, configuration_problem=lambda: "未登录")
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: module}):
            self.assertEqual(_background_launcher_problem(), "未登录")

    def test_configuration_problem_exception_does_not_leak_message(self):
        def boom():
            raise RuntimeError("/Users/secret/path sk-12345")

        module = fake_module(LAUNCHER_MODULE, configuration_problem=boom)
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: module}):
            problem = _background_launcher_problem()
        self.assertEqual(problem, "后台启动器配置检查失败：RuntimeError")
        self.assertNotIn("secret", problem)

    def test_module_without_check_is_ok(self):
        module = fake_module(LAUNCHER_MODULE)
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: module}):
            self.assertIsNone(_background_launcher_problem())


class RecordingRunner:
    def __init__(self, paths, adapter_id, provider):
        self.paths, self.adapter_id, self.provider = paths, adapter_id, provider


class PublicRunnerTests(ProjectCase):
    RULINGS = {"claude-code": "background"}

    def bg_card(self):
        with missing_launcher():
            nodes = [make_node("n1", ["a.py"]), make_node("n2", ["b.py"])]
            entries = _background_worker_entries(nodes, self.RULINGS)
        return make_card(self.RULINGS, entries)

    def test_background_card_gets_background_runner(self):
        paths = self.project({BG: True})
        _plan, nodes, card = self.bg_card()
        module = fake_module(RUNNER_MODULE, ClaudeBgRunner=RecordingRunner)
        with mock.patch.dict(sys.modules, {RUNNER_MODULE: module}):
            runner = _public_runner(paths, card, nodes)
        self.assertIsInstance(runner, RecordingRunner)
        self.assertEqual(runner.adapter_id, "claude-code")
        self.assertEqual(runner.provider, "claude-code-background")
        self.assertIs(runner.paths, paths)

    def test_missing_runner_module_reports_not_installed(self):
        paths = self.project({BG: True})
        _plan, nodes, card = self.bg_card()
        with mock.patch.dict(sys.modules, {RUNNER_MODULE: None}):
            with self.assertRaises(ValueError) as caught:
                _public_runner(paths, card, nodes)
        self.assertIn("后台运行器未安装", str(caught.exception))

    def test_old_card_keeps_visible_runner(self):
        paths = self.project({BG: True, P + "in_session_sdd": True})
        _plan, nodes, old_card = make_card()
        module = fake_module(RUNNER_MODULE, ClaudeBgRunner=RecordingRunner)
        with mock.patch.dict(sys.modules, {RUNNER_MODULE: module}):
            runner = _public_runner(paths, old_card, nodes)
        self.assertIsInstance(runner, ProviderActionRunner)
        self.assertEqual(runner.provider, "claude-code-visible")

    def test_old_card_with_visible_facts_keeps_visible_runner(self):
        visible = {P + "visible_task." + s: True for s in ("create", "enter", "resume", "wait")}
        paths = self.project({BG: True, **visible})
        _plan, nodes, old_card = make_card()
        runner = _public_runner(paths, old_card, nodes)
        self.assertIsInstance(runner, ProviderActionRunner)
        self.assertEqual(runner.provider, "claude-code-visible")

    def test_old_card_without_visible_or_sdd_is_refused(self):
        paths = self.project({BG: True})
        _plan, nodes, old_card = make_card()
        with self.assertRaises(ValueError) as caught:
            _public_runner(paths, old_card, nodes)
        self.assertIn("no verified dispatch lifecycle", str(caught.exception))

    def test_no_background_fact_path_is_unchanged(self):
        paths = self.project({P + "in_session_sdd": True})
        _plan, nodes, card = make_card()
        runner = _public_runner(paths, card, nodes)
        self.assertIsInstance(runner, ProviderActionRunner)
        self.assertEqual(runner.provider, "claude-code-visible")


class RegistryLauncherTests(unittest.TestCase):
    def test_construction_never_imports_claude_bg(self):
        with mock.patch.dict(sys.modules):
            sys.modules.pop(LAUNCHER_MODULE, None)
            real = importlib.import_module
            seen = []

            def recording(name, *args, **kwargs):
                seen.append(name)
                return real(name, *args, **kwargs)

            with mock.patch("importlib.import_module", side_effect=recording):
                AdapterRegistry()
            self.assertNotIn(LAUNCHER_MODULE, seen)
            self.assertNotIn(LAUNCHER_MODULE, sys.modules)

    def test_default_launcher_is_a_lazy_proxy_for_claude_code_only(self):
        registry = AdapterRegistry()
        self.assertTrue(callable(registry.get("claude-code").background_launcher.launch))
        self.assertIsNone(registry.get("codex").background_launcher)

    def test_proxy_raises_provider_unavailable_when_module_missing(self):
        launcher = AdapterRegistry().get("claude-code").background_launcher
        with missing_launcher():
            with self.assertRaises(ProviderUnavailable) as caught:
                launcher.launch("developer", "n1", Path("/tmp/contract.json"))
        self.assertIn("verified background launcher is not configured", str(caught.exception))

    def test_proxy_delegates_to_launcher_class(self):
        calls = []

        class Launcher:
            def launch(self, role, issue_id, contract_path):
                calls.append((role, issue_id, contract_path))
                return {"ok": True}

        module = fake_module(LAUNCHER_MODULE, ClaudeBgLauncher=Launcher)
        launcher = AdapterRegistry().get("claude-code").background_launcher
        with mock.patch.dict(sys.modules, {LAUNCHER_MODULE: module}):
            result = launcher.launch("developer", "n1", Path("/tmp/c.json"))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [("developer", "n1", Path("/tmp/c.json"))])

    def test_explicit_launchers_are_used_verbatim(self):
        sentinel = object()
        registry = AdapterRegistry(background_launchers={"codex": sentinel})
        self.assertIs(registry.get("codex").background_launcher, sentinel)
        self.assertIsNone(registry.get("claude-code").background_launcher)
        empty = AdapterRegistry(background_launchers={})
        self.assertIsNone(empty.get("claude-code").background_launcher)


if __name__ == "__main__":
    unittest.main()
