"""Monitor may widen a node's file scope on its own only for mechanical files.

Approved rule: a correction may pull a file into a node's scope without asking
the product manager only when the file is inside the repository and is either
under ``tests/`` or listed in ``.vibe/config.json`` ``auto_scope_paths``; it
must not be a deploy/credential file and must not be held by another active
(not yet accepted) node.  Every such widening is recorded.  Anything else
still stops for the user.
"""
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.config import load_project_config
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor, ScopeDecisionRequired
from vibe_guide.paths import ProjectPaths
from vibe_guide.planner import resolve_consistency, resolve_scope_expansion
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.cli import _snapshot_result, run_cli
from vibe_guide.models import INTEGRATION_REVIEW_NODE_ID
from vibe_guide.dag import append_integration_review_node
from vibe_guide.engine_attestation import create_engine_attestation
from vibe_guide.planner import REQUIRED_COMPLEX_WORKFLOW, TaskContext
from vibe_guide.task_registry import TaskBinding
from vibe_guide.workflow_gate import create_task_workflow, record_workflow_node
from vibe_guide.state import durable_projection, load_events, load_snapshot, save_snapshot


BINDING = {
    "schema_version": 1,
    "project_digest": "1" * 64,
    "plan_id": "plan-1",
    "plan_version": 1,
    "decision_digest": "2" * 64,
    "authorization_digest": "3" * 64,
    "issue_contract_digest": "4" * 64,
}
DECISION = {
    "id": "decision-naming",
    "field": "naming",
    "revision": 1,
    "status": "approved",
    "selected": "approved-name",
}


def _inconsistency(files, binding=BINDING):
    return {
        "field": "naming",
        "action": "rework",
        "files": list(files),
        "candidates": [
            {
                "source": "approved_prd",
                "value": "approved-name",
                "binding": binding,
                "decision": dict(DECISION),
            },
            {"source": "implementation", "value": "stale-name"},
        ],
    }


def _scope_claim(files, reason="approved examples keep the same result"):
    return {
        "files": list(files),
        "acceptance_examples_unchanged": True,
        "reason": reason,
        "adds_third_party_dependency": False,
        "visibility_downgrade": False,
    }


CLEARED_INTEGRATION_CLAIM = {
    "findings": [],
    "iteration_compatibility": {
        "status": "verified",
        "evidence": "iteration compatibility checked",
    },
    "test_runtime_delivery": {
        "status": "verified",
        "evidence": "tests and runtime delivery checked",
    },
    "out_of_scope": [],
}


def _resolve(files, node_files=("n1.py",), auto_scope_paths=(), occupied_files=()):
    return resolve_consistency(
        _inconsistency(files),
        decisions=[dict(DECISION)],
        issue_contract={"naming": "approved-name"},
        authorized_actions=["rework"],
        authorized_files=["n1.py"],
        expected_binding=BINDING,
        node_files=list(node_files),
        auto_scope_paths=tuple(auto_scope_paths),
        occupied_files=tuple(occupied_files),
    )


class ResolveConsistencyAutoScopeTests(unittest.TestCase):
    def test_test_file_outside_scope_is_expanded(self):
        result = _resolve(["n1.py", "tests/test_n1.py"])
        self.assertIsNotNone(result)
        self.assertEqual(result.scope_expanded_files, ["tests/test_n1.py"])

    def test_listed_mechanical_file_is_expanded(self):
        result = _resolve(
            ["n1.py", "vibe_guide/protocols/__init__.py"],
            auto_scope_paths=["vibe_guide/protocols/__init__.py"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            result.scope_expanded_files, ["vibe_guide/protocols/__init__.py"]
        )

    def test_unlisted_non_test_file_still_requires_user(self):
        self.assertIsNone(_resolve(["n1.py", "vibe_guide/protocols/__init__.py"]))
        self.assertIsNone(
            _resolve(
                ["n1.py", "vibe_guide/other.py"],
                auto_scope_paths=["vibe_guide/protocols/__init__.py"],
            )
        )

    def test_file_held_by_another_active_node_is_refused(self):
        self.assertIsNone(
            _resolve(
                ["n1.py", "tests/test_shared.py"],
                occupied_files=["tests/test_shared.py"],
            )
        )

    def test_card_scope_of_a_finished_node_stays_in_scope(self):
        """Files the card already authorized keep working as before this change."""
        result = resolve_consistency(
            _inconsistency(["n1.py", "n2.py"]),
            decisions=[dict(DECISION)],
            issue_contract={"naming": "approved-name"},
            authorized_actions=["rework"],
            authorized_files=["n1.py", "n2.py"],
            expected_binding=BINDING,
            node_files=["n1.py"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.scope_expanded_files, [])

    def test_card_file_another_active_node_writes_is_refused(self):
        self.assertIsNone(resolve_consistency(
            _inconsistency(["n1.py", "n2.py"]),
            decisions=[dict(DECISION)],
            issue_contract={"naming": "approved-name"},
            authorized_actions=["rework"],
            authorized_files=["n1.py", "n2.py"],
            expected_binding=BINDING,
            node_files=["n1.py"],
            occupied_files=["n2.py"],
        ))

    def test_path_traversal_and_absolute_paths_are_refused(self):
        for path in (
            "tests/../vibe_guide/monitor.py",
            "../tests/test_n1.py",
            "/tmp/tests/test_n1.py",
            "tests/./test_n1.py",
            "tests//test_n1.py",
            "tests\\test_n1.py",
            "tests",
            "tests/",
        ):
            with self.subTest(path=path):
                self.assertIsNone(_resolve(["n1.py", path]))
        self.assertIsNone(
            _resolve(["n1.py", "../x.py"], auto_scope_paths=["../x.py"])
        )

    def test_credential_like_files_under_tests_are_refused(self):
        for path in ("tests/.env", "tests/fixtures/server.pem", "tests/id.key"):
            with self.subTest(path=path):
                self.assertIsNone(_resolve(["n1.py", path]))

    def test_in_scope_files_produce_no_expansion(self):
        result = _resolve(["n1.py"])
        self.assertIsNotNone(result)
        self.assertEqual(result.scope_expanded_files, [])

    def test_approved_acceptance_boundary_expands_safe_repository_file(self):
        result = resolve_scope_expansion(
            _scope_claim(["n1.py", "vibe_guide/protocols/__init__.py"]),
            authorized_files=["n1.py"],
            node_files=["n1.py"],
            acceptance_rule_approved=True,
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            result.files, ["vibe_guide/protocols/__init__.py"]
        )
        self.assertEqual(
            result.scope_rules,
            [{"path": "vibe_guide/protocols/__init__.py", "rule": "acceptance_unchanged"}],
        )

    def test_claim_is_held_to_mechanical_rule_until_project_approves(self):
        # Integrated PRD N5 / AC-B1a: before the AGENTS.md §6 patch is
        # approved, an acceptance-unchanged claim cannot widen scope past
        # tests/ and auto_scope_paths.
        for flag in (False, None, "true", 1):
            with self.subTest(flag=flag):
                kwargs = {} if flag is None else {"acceptance_rule_approved": flag}
                self.assertIsNone(resolve_scope_expansion(
                    _scope_claim(["n1.py", "vibe_guide/protocols/__init__.py"]),
                    authorized_files=["n1.py"], node_files=["n1.py"], **kwargs,
                ))
        held = resolve_scope_expansion(
            _scope_claim(["n1.py", "tests/test_n1.py"]),
            authorized_files=["n1.py"], node_files=["n1.py"],
        )
        self.assertEqual(held.files, ["tests/test_n1.py"])

    def test_product_boundaries_and_external_files_never_auto_expand(self):
        blocked = (
            _scope_claim(["pyproject.toml"]),
            _scope_claim([".github/workflows/test.yml"]),
            _scope_claim(["deploy/prod.yaml"]),
            dict(_scope_claim(["safe.py"]), adds_third_party_dependency=True),
            dict(_scope_claim(["safe.py"]), visibility_downgrade=True),
            dict(_scope_claim(["safe.py"]), acceptance_examples_unchanged=False),
            dict(_scope_claim(["safe.py"]), reason=""),
        )
        for claim in blocked:
            with self.subTest(claim=claim):
                self.assertIsNone(
                    resolve_scope_expansion(
                        claim,
                        authorized_files=["n1.py"],
                        node_files=["n1.py"],
                    )
                )

    def test_scope_reason_rejects_control_characters_and_excessive_length(self):
        for reason in (
            "line one\nline two",
            "tab\tseparated",
            "hidden\u202etext",
            "x" * 201,
        ):
            with self.subTest(reason=repr(reason)):
                self.assertIsNone(
                    resolve_scope_expansion(
                        _scope_claim(["safe.py"], reason=reason),
                        authorized_files=["n1.py"],
                        node_files=["n1.py"],
                    )
                )

    def test_scope_reason_is_redacted_in_durable_projection(self):
        projected = durable_projection(
            {"scope_reason": "token=super-secret password=hunter2"}
        )
        self.assertEqual(projected["scope_reason"], "[REDACTED_PROVIDER_TEXT]")

    def test_sensitive_scope_aliases_remain_fail_closed(self):
        blocked = (
            "Secrets/Prod.yaml",
            "credentials/rotation.json",
            "REQUIREMENTS-prod.in",
            "Pipfile.lock",
            "Docker-Compose.Override.yml",
            "Dockerfile.prod",
            "deployments/blue.yaml",
        )
        for path in blocked:
            with self.subTest(path=path):
                self.assertIsNone(
                    resolve_scope_expansion(
                        _scope_claim([path]),
                        authorized_files=["n1.py"],
                        node_files=["n1.py"],
                    )
                )

    def test_common_credential_dependency_and_deployment_aliases_remain_fail_closed(self):
        blocked = (
            ".ssh/config",
            "secret-prod.yaml",
            "prod.env",
            "deployment.yaml",
            "helmfile.yaml",
            "kustomization.yaml",
            "constraints-prod.txt",
            ".SSH/CONFIG",
            "SECRET-PROD.YAML",
            "PROD.ENV",
            "DEPLOYMENT.YAML",
            "HELMFILE.YAML",
            "KUSTOMIZATION.YAML",
            "CONSTRAINTS-prod.TXT",
        )
        for path in blocked:
            with self.subTest(path=path):
                self.assertIsNone(
                    resolve_scope_expansion(
                        _scope_claim([path]),
                        authorized_files=["n1.py"],
                        node_files=["n1.py"],
                    )
                )

    def test_container_infra_tooling_and_config_aliases_remain_fail_closed(self):
        blocked = (
            "compose.yaml",
            "compose.yml",
            "Chart.yaml",
            "values.yaml",
            "skaffold.yaml",
            "Tiltfile",
            "terraform.tfvars",
            "prod.tfvars",
            ".config/gcloud/credentials.db",
            "pip-compile.txt",
            "setup.cfg",
            "tox.ini",
            "COMPOSE.YAML",
            "chart.yaml",
            "values.YAML",
            "SKAFFOLD.YAML",
            "tiltfile",
            "TERRAFORM.TFVARS",
            "PROD.TFVARS",
            ".CONFIG/GCLOUD/CREDENTIALS.DB",
            "PIP-COMPILE.TXT",
            "SETUP.CFG",
            "TOX.INI",
        )
        for path in blocked:
            with self.subTest(path=path):
                self.assertIsNone(
                    resolve_scope_expansion(
                        _scope_claim([path]),
                        authorized_files=["n1.py"],
                        node_files=["n1.py"],
                    )
                )

    def test_unknown_conventional_sensitive_paths_remain_fail_closed(self):
        blocked = (
            ".envrc",
            "kubeconfig",
            "application-default-credentials.json",
            "Pulumi.yaml",
            "serverless.yml",
            "fly.toml",
            "vercel.json",
            "cloudbuild.yaml",
            "buildspec.yml",
            "pylock.toml",
            "pom.xml",
            "build.gradle",
            "unknown.artifact",
        )
        for path in blocked:
            with self.subTest(path=path):
                self.assertIsNone(
                    resolve_scope_expansion(
                        _scope_claim([path]),
                        authorized_files=["n1.py"],
                        node_files=["n1.py"],
                    )
                )

    def test_affirmative_low_risk_allowlist_accepts_source_and_document_paths(self):
        for path in ("src/module.py", "docs/notes.md", "lib/helper.rs"):
            with self.subTest(path=path):
                result = resolve_scope_expansion(
                    _scope_claim([path]),
                    authorized_files=["n1.py"],
                    node_files=["n1.py"],
                    acceptance_rule_approved=True,
                )
                self.assertIsNotNone(result)
                self.assertEqual(result.files, [path])
                self.assertEqual(
                    result.scope_rules,
                    [{"path": path, "rule": "acceptance_unchanged"}],
                )

    def test_agent_rule_files_never_auto_expand(self):
        for path in ("AGENTS.md", "docs/CLAUDE.md", "CODEBUDDY.md", "CLAUDE.local.md",
                     ".github/copilot-instructions.md", ".cursor/rules/a.md",
                     ".vibe/proposals/skills/prd-guide/SKILL.md", ".claude/agents/x.md"):
            with self.subTest(path=path):
                self.assertIsNone(resolve_scope_expansion(
                    _scope_claim([path]), authorized_files=["n1.py"],
                    node_files=["n1.py"], acceptance_rule_approved=True,
                ))

    def test_conventional_sensitive_names_are_refused_even_with_source_suffix(self):
        blocked = (
            "setup.py",
            "requirements.py",
            "build.py",
            "kubeconfig.py",
            "terraform.py",
            "compose.py",
            "SETUP.PY",
            "nested/Requirements.py",
            "nested/build.PY",
            "nested/KUBECONFIG.Py",
            "nested/terraform.PY",
            "nested/COMPOSE.PY",
        )
        for path in blocked:
            with self.subTest(path=path):
                self.assertIsNone(
                    resolve_scope_expansion(
                        _scope_claim([path]),
                        authorized_files=["n1.py"],
                        node_files=["n1.py"],
                    )
                )


class AutoScopeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, data):
        (self.root / ".vibe").mkdir(exist_ok=True)
        (self.root / ".vibe" / "config.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_default_list_is_empty(self):
        self.assertEqual(load_project_config(self.root).auto_scope_paths, ())
        self._write({"max_active_worker_sessions": 3})
        self.assertEqual(load_project_config(self.root).auto_scope_paths, ())

    def test_configured_list_is_loaded(self):
        self._write({"auto_scope_paths": ["vibe_guide/protocols/__init__.py"]})
        self.assertEqual(
            load_project_config(self.root).auto_scope_paths,
            ("vibe_guide/protocols/__init__.py",),
        )

    def test_invalid_entries_are_configuration_errors(self):
        for value in ("a.py", ["/abs.py"], ["../x.py"], ["a/../b.py"], [""], [1]):
            with self.subTest(value=value):
                self._write({"auto_scope_paths": value})
                with self.assertRaisesRegex(ValueError, "auto_scope_paths"):
                    load_project_config(self.root)
    def test_control_characters_in_config_are_errors(self):
        for value in (["a\nb.py"], ["a\x00.py"], ["./a.py"], ["a//b.py"], [" a.py"]):
            with self.subTest(value=repr(value)):
                self._write({"auto_scope_paths": value})
                with self.assertRaisesRegex(ValueError, "auto_scope_paths"):
                    load_project_config(self.root)



class ReviewRoundOneUnitTests(unittest.TestCase):
    """PR #144 round-one review findings, at the resolve_consistency level."""

    def test_own_files_are_never_removed_by_occupancy(self):
        # P1-1: another node's scope (e.g. the integration reviewer's union)
        # listing this node's own file must not push it out of scope.
        result = _resolve(["n1.py", "tests/test_n1.py"], occupied_files=["n1.py"])
        self.assertIsNotNone(result)
        self.assertEqual(result.scope_expanded_files, ["tests/test_n1.py"])

    def test_occupancy_uses_normalized_prefix_overlap(self):
        # P1-2: directory scopes and ./ spellings still count as held.
        for held in (["tests"], ["./tests/test_shared.py"], ["tests/"]):
            with self.subTest(held=held):
                self.assertIsNone(
                    _resolve(["n1.py", "tests/test_shared.py"], occupied_files=held)
                )
        self.assertIsNone(resolve_consistency(
            _inconsistency(["n1.py", "n2.py"]),
            decisions=[dict(DECISION)],
            issue_contract={"naming": "approved-name"},
            authorized_actions=["rework"],
            authorized_files=["n1.py", "n2.py"],
            expected_binding=BINDING,
            node_files=["n1.py"],
            occupied_files=["./n2.py"],
        ))

    def test_occupancy_ignores_case_and_unicode_form(self):
        # P2-1: macOS treats these as the same file.
        self.assertIsNone(
            _resolve(["n1.py", "tests/test_shared.py"],
                     occupied_files=["tests/Test_Shared.py"])
        )
        nfd = "tests/cafe\u0301.py"
        nfc = "tests/caf\u00e9.py"
        self.assertIsNone(_resolve(["n1.py", nfc], occupied_files=[nfd]))
        # Comparison only: the stored spelling stays as written.
        result = _resolve(["n1.py", "tests/Test_N1.py"], occupied_files=["tests/other.py"])
        self.assertEqual(result.scope_expanded_files, ["tests/Test_N1.py"])

    def test_control_characters_are_refused(self):
        # P2-2
        for path in ("tests/a\x00.py", "tests/a\nb.py", "tests/a\tb.py", "tests/a\x7f.py"):
            with self.subTest(path=repr(path)):
                self.assertIsNone(_resolve(["n1.py", path]))

    def test_colon_paths_are_refused(self):
        # P2-A: `C:foo` is drive-relative on Windows; `a:b.py` is an NTFS
        # alternate data stream.
        for path in ("tests/C:foo.py", "tests/a:b.py"):
            with self.subTest(path=path):
                self.assertIsNone(_resolve(["n1.py", path]))
        self.assertIsNone(_resolve(["n1.py", "C:foo"], auto_scope_paths=["C:foo"]))

    def test_format_characters_and_git_segments_are_refused(self):
        # P3: zero-width / bidi controls (Unicode Cf) and `.git` segments.
        for path in (
            "tests/test_\u200bn1.py", "tests/test_\u202en1.py",
            "tests/\ufefftest_n1.py", "tests/.git/config", "tests/x/.git/hooks/pre-commit",
        ):
            with self.subTest(path=repr(path)):
                self.assertIsNone(_resolve(["n1.py", path]))
        self.assertIsNone(_resolve(["n1.py", ".git/config"], auto_scope_paths=[".git/config"]))

    def test_glob_occupancy_entries_hold_everything(self):
        # P3: a glob cannot be matched safely, so it fails closed.
        for held in (["tests/*.py"], ["tests/test_?.py"], ["tests/[ab].py"]):
            with self.subTest(held=held):
                self.assertIsNone(_resolve(["n1.py", "vibe_guide/protocols/__init__.py"],
                                           auto_scope_paths=["vibe_guide/protocols/__init__.py"],
                                           occupied_files=held))

    def test_more_credential_names_are_refused(self):
        # P3-1
        for path in (
            "tests/fixtures/id_rsa", "tests/fixtures/id_ed25519",
            "tests/fixtures/id_ecdsa", "tests/fixtures/server.crt",
            "tests/fixtures/secrets.yaml", "tests/fixtures/service-account-ci.json",
        ):
            with self.subTest(path=path):
                self.assertIsNone(_resolve(["n1.py", path]))


def _node(node_id, extra_files=()):
    files = [node_id + ".py", *extra_files]
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "g1",
        {
            "files": files,
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "naming": "approved-name",
            "worker_profile": {
                "worker": "codex", "model": "test", "reasoning": "normal",
                "fallbacks": [],
                "selection_basis": {
                    "issue_complexity_ref": node_id, "complexity_band": "standard",
                    "risk_tags": [], "availability_evidence": "test",
                },
                "writer": "writer", "worktree": ".worktrees/" + node_id,
                "branch": "branch-" + node_id, "allowlist": list(files),
            },
        },
        "ready",
    )


class _BoundBindingRunner(FakeRunner):
    """Visible binding with host/cursor evidence (as in the issue-88 tests)."""

    def task_binding(self, contract, worktree, run_id, status):
        return TaskBinding(
            provider="fake", mode="visible", issue_id=contract["node_id"],
            role=contract["role"],
            task_id="task-{}-{}".format(contract["node_id"], contract["role"]),
            host="host-1", worktree=str(worktree),
            branch=str(contract.get("branch", "")), run_id=run_id,
            status=status, cursor="cursor-1", hostId="host-1",
            generation=contract["generation"],
        )


_REAL_START_TASK = Monitor._start_task


def _interrupt_rework(monitor, snapshot, node_id, role, phase, *args, **kwargs):
    if phase == "rework":
        raise RuntimeError("interrupted after scope event")
    return _REAL_START_TASK(monitor, snapshot, node_id, role, phase, *args, **kwargs)


class MonitorAutoScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n',
            encoding="utf-8",
        )
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities(
            "fake", True, True, True, True, True, "full"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _approve_acceptance_rule(self):
        (self.paths.vibe / "config.json").write_text(
            json.dumps({"acceptance_scope_rule_approved": True}), encoding="utf-8"
        )

    def _run_finding(self, nodes, files):
        plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft",
            decisions=[dict(DECISION, question="canonical name")],
        )
        card = build_authorization_card(plan, nodes, self.capabilities)
        monitor = Monitor(self.paths, plan, nodes)
        return self._drive(monitor, card, nodes, files)

    def _drive(self, monitor, card, nodes, files, runner_class=FakeRunner,
               delivery=("complete", {"evidence": "delivery"}),
               finding_overrides=None):
        self._monitor = monitor
        runner = runner_class(
            events={
                (item.id, "developer"): [delivery]
                for item in nodes
                if item.id != INTEGRATION_REVIEW_NODE_ID
            }
        )
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        self._run_id = snapshot.run_id
        snapshot = monitor.tick(snapshot.run_id, runner)
        binding = [
            call for call in runner.start_calls if call["node_id"] == "n1"
        ][-1]["consistency_binding"]
        finding = {
            "finding": "fixture missing",
            "in_contract": False,
            "consistency": _inconsistency(files, binding),
        }
        overrides = (
            finding_overrides(binding)
            if callable(finding_overrides)
            else finding_overrides
        )
        finding.update(overrides or {})
        runner.events[("n1", "reviewer")] = [
            (
                "review_finding",
                finding,
            )
        ]
        snapshot = monitor.tick(snapshot.run_id, runner)
        return monitor, runner, snapshot

    def _build_complex_plan(self, business):
        """Complex plan: vibe appends the read-only integration reviewer.

        Fixture mirrors tests/test_issue_88_delivery_gate.py (workflow
        evidence, PRD/Spec files, engine attestation)."""
        (self.paths.root / "docs").mkdir(parents=True, exist_ok=True)
        (self.paths.root / "docs" / "prd.md").write_text("prd\n", encoding="utf-8")
        (self.paths.root / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
        workflow = create_task_workflow("plan-1", TaskContext(5, 5, 5, 5, 5))
        plan_dir = self.paths.vibe / "plans" / "plan-1"
        plan_dir.mkdir(parents=True, exist_ok=True)
        evidence_file = plan_dir / "workflow-evidence.md"
        evidence_file.write_text("evidence\n", encoding="utf-8")
        artifact = {
            "ref": evidence_file.name,
            "sha256": hashlib.sha256(evidence_file.read_bytes()).hexdigest(),
        }
        for node_id in REQUIRED_COMPLEX_WORKFLOW:
            record_workflow_node(
                workflow, node_id, {"ref": node_id}, {"ref": node_id},
                {"ref": node_id, "artifact": artifact},
            )
        (self.paths.vibe / "state.json").write_text(
            json.dumps({"workflow_version": 2, "session_gate": "s0_required",
                        "task_workflow": workflow}),
            encoding="utf-8",
        )
        plan = append_integration_review_node(Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in business], "draft",
            decisions=[dict(DECISION, question="canonical name")],
            spec_path="docs/spec.md",
            complexity_band="complex",
            nodes=list(business),
            integration_contract={
                "iteration_context": {"kind": "iteration", "based_on": "V5"},
                "compatibility_scope": ["V5 API"],
                "agentsmd_acceptance_refs": ["AGENTS.md#8"],
                "integration_acceptance_contract": {"checks": ["all"]},
                "unverified_or_excluded": ["provider"],
            },
        ))
        nodes = list(plan.nodes)
        attestation = create_engine_attestation(
            plan_id=plan.plan_id, plan_revision=plan.version,
            execution_engine="vibeguide_monitor", engine_mode="dag",
            provider="fake", capability_facts={"fake.worktree": True},
            provenance="live:test",
            now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        (plan_dir / "engine-attestation.json").write_text(
            json.dumps(attestation), encoding="utf-8"
        )
        card = build_authorization_card(
            plan, nodes, self.capabilities,
            execution_engine="vibeguide_monitor", engine_mode="dag",
            engine_evidence_ref=attestation["evidence_ref"],
        )
        monitor = Monitor(self.paths, plan, nodes)
        # Topology projection evidence is a separate gate (same isolation as
        # the issue-88 fixture).
        monitor._validate_execution_topology = lambda snapshot: None
        delivered = ("delivered", {
            "evidence": "delivery",
            "delivery_evidence": {
                "completion_marker": "DONE", "delivery_path": "/out",
                "thread_status": "complete",
            },
        })
        return plan, nodes, card, monitor, delivered

    def _run_complex_finding(self, business, files):
        plan, nodes, card, monitor, delivered = self._build_complex_plan(business)
        return plan, self._drive(
            monitor, card, nodes, files, _BoundBindingRunner, delivered
        )

    def _scope_events(self, run_id):
        return [
            record["data"] for record in load_events(self.paths, run_id)
            if record["event"] == "scope_auto_expanded"
        ]

    def test_test_file_is_expanded_into_rework_scope_and_recorded(self):
        _monitor, runner, snapshot = self._run_finding(
            [_node("n1")], ["n1.py", "tests/test_n1.py"]
        )
        current = snapshot.nodes["n1"]
        self.assertEqual(current["status"], "rework")
        self.assertEqual(current["scope_expansions"], ["tests/test_n1.py"])
        rework = [c for c in runner.start_calls if c["node_id"] == "n1"][-1]
        self.assertEqual(rework["phase"], "rework")
        self.assertIn("tests/test_n1.py", rework["files"])
        events = self._scope_events(snapshot.run_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0],
            {
                "run_id": snapshot.run_id,
                "node_id": "n1",
                "files": ["tests/test_n1.py"],
                "scope_rules": [{"path": "tests/test_n1.py", "rule": "tests_dir"}],
            },
        )

    def test_listed_mechanical_file_is_expanded_and_recorded(self):
        (self.paths.vibe / "config.json").write_text(
            json.dumps({"auto_scope_paths": ["vibe_guide/protocols/__init__.py"]}),
            encoding="utf-8",
        )
        _monitor, runner, snapshot = self._run_finding(
            [_node("n1")], ["n1.py", "vibe_guide/protocols/__init__.py"]
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        events = self._scope_events(snapshot.run_id)
        self.assertEqual(
            events[0]["scope_rules"],
            [{"path": "vibe_guide/protocols/__init__.py", "rule": "auto_scope_paths"}],
        )

    def test_implementation_finding_uses_the_same_scope_expansion_entry(self):
        self._approve_acceptance_rule()
        files = ["n1.py", "vibe_guide/protocols/__init__.py"]
        plan = Plan(
            "plan-1", 1, "docs/prd.md", ["n1"], "draft",
            decisions=[dict(DECISION, question="canonical name")],
        )
        nodes = [_node("n1")]
        monitor = Monitor(self.paths, plan, nodes)
        card = build_authorization_card(plan, nodes, self.capabilities)
        _monitor, runner, snapshot = self._drive(
            monitor,
            card,
            nodes,
            files,
            finding_overrides=lambda binding: {
                "severity": "P1",
                "consistency": dict(
                    _inconsistency(files, binding),
                    scope_expansion=_scope_claim(files),
                ),
            },
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertIn("vibe_guide/protocols/__init__.py", runner.start_calls[-1]["files"])
        self.assertEqual(
            self._scope_events(snapshot.run_id)[0]["scope_rules"],
            [{"path": "vibe_guide/protocols/__init__.py", "rule": "acceptance_unchanged"}],
        )

    def test_accepted_patch_queues_same_node_rework_and_records_scope(self):
        self._approve_acceptance_rule()
        node = _node("n1")
        plan = Plan(
            "plan-1", 1, "docs/prd.md", [node.id], "draft",
            decisions=[dict(DECISION, question="canonical name")],
        )
        monitor = Monitor(self.paths, plan, [node])
        runner = FakeRunner(events={
            ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        snapshot = monitor.start(
            authorize(build_authorization_card(plan, [node], self.capabilities), "AUTHORIZE"),
            runner,
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")

        snapshot = monitor.request_scope_expansion(
            snapshot.run_id,
            "n1",
            _scope_claim(["vibe_guide/protocols/__init__.py"]),
            trigger="accepted_patch",
        )

        current = snapshot.nodes["n1"]
        self.assertEqual(current["status"], "rework")
        self.assertIsNone(current["acceptance"])
        self.assertTrue(current["retryable_action"]["continuation"])
        self.assertEqual(
            self._scope_events(snapshot.run_id)[0]["trigger"], "accepted_patch"
        )

    def test_accepted_patch_without_approved_rule_asks_product_manager(self):
        # Integrated PRD N5 / AC-B1a at the monitor level.
        node = _node("n1")
        plan = Plan(
            "plan-1", 1, "docs/prd.md", [node.id], "draft",
            decisions=[dict(DECISION, question="canonical name")],
        )
        monitor = Monitor(self.paths, plan, [node])
        runner = FakeRunner(events={
            ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        snapshot = monitor.start(
            authorize(build_authorization_card(plan, [node], self.capabilities), "AUTHORIZE"),
            runner,
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        with self.assertRaises(ScopeDecisionRequired):
            monitor.request_scope_expansion(
                snapshot.run_id, "n1",
                _scope_claim(["vibe_guide/protocols/__init__.py"]),
                trigger="accepted_patch",
            )
        self.assertEqual(self._scope_events(snapshot.run_id), [])

    def test_accepted_patch_persists_redacted_scope_reason(self):
        self._approve_acceptance_rule()
        node = _node("n1")
        plan = Plan(
            "plan-1", 1, "docs/prd.md", [node.id], "draft",
            decisions=[dict(DECISION, question="canonical name")],
        )
        monitor = Monitor(self.paths, plan, [node])
        runner = FakeRunner(events={
            ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        snapshot = monitor.start(
            authorize(build_authorization_card(plan, [node], self.capabilities), "AUTHORIZE"),
            runner,
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        monitor.request_scope_expansion(
            snapshot.run_id,
            "n1",
            _scope_claim(
                ["vibe_guide/protocols/__init__.py"],
                reason="token=super-secret password=hunter2",
            ),
        )
        self.assertEqual(
            self._scope_events(snapshot.run_id)[0]["scope_reason"],
            "[REDACTED_PROVIDER_TEXT]",
        )

    def test_completed_run_patch_invalidates_and_requeues_integration_review(self):
        self._approve_acceptance_rule()
        plan, nodes, card, monitor, delivered = self._build_complex_plan(
            [_node("n1")]
        )
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "developer"): [delivered],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [(
                "accepted", {"evidence": dict(CLEARED_INTEGRATION_CLAIM)}
            )],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.status, "complete")
        integration_calls = [call for call in runner.start_calls
                             if call["node_id"] == INTEGRATION_REVIEW_NODE_ID]
        self.assertEqual([call["role"] for call in integration_calls], ["reviewer"])
        self.assertTrue(snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["pair_archived"])

        snapshot = monitor.request_scope_expansion(
            snapshot.run_id,
            "n1",
            _scope_claim(["vibe_guide/protocols/__init__.py"]),
        )

        integration = snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]
        self.assertEqual(snapshot.status, "running")
        self.assertEqual(integration["status"], "rework")
        self.assertIsNone(integration["acceptance"])
        self.assertNotIn("review_clearance", integration)
        self.assertEqual(integration["retryable_action"]["role"], "reviewer")
        self.assertEqual(
            integration["retryable_action"]["phase"], "review"
        )
        self.assertNotIn("clearance", snapshot.integration_review_evidence)
        self.assertEqual(len(snapshot.integration_review_evidence["history"]), 1)

    def test_accepted_patch_refuses_the_read_only_integration_node(self):
        plan, nodes, card, monitor, delivered = self._build_complex_plan(
            [_node("n1")]
        )
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "developer"): [delivered],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [(
                "accepted", {"evidence": dict(CLEARED_INTEGRATION_CLAIM)}
            )],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)

        with self.assertRaisesRegex(ValueError, "business node"):
            monitor.request_scope_expansion(
                snapshot.run_id,
                INTEGRATION_REVIEW_NODE_ID,
                _scope_claim(["tests/test_integration_patch.py"]),
            )

    def test_accepted_patch_queue_rework_replays_after_snapshot_write_failure(self):
        self._approve_acceptance_rule()
        node = _node("n1")
        plan = Plan(
            "plan-1", 1, "docs/prd.md", [node.id], "draft",
            decisions=[dict(DECISION, question="canonical name")],
        )
        monitor = Monitor(self.paths, plan, [node])
        runner = FakeRunner(events={
            ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        snapshot = monitor.start(
            authorize(build_authorization_card(plan, [node], self.capabilities), "AUTHORIZE"),
            runner,
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        with patch("vibe_guide.monitor.save_snapshot", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                monitor.request_scope_expansion(
                    snapshot.run_id,
                    "n1",
                    _scope_claim(["vibe_guide/protocols/__init__.py"]),
                )

        recovered = monitor.resume(snapshot.run_id, FakeRunner(), poll_handles=False)
        current = recovered.nodes["n1"]
        self.assertEqual(current["status"], "rework")
        self.assertEqual(
            current["scope_expansions"], ["vibe_guide/protocols/__init__.py"]
        )
        self.assertEqual(current["retryable_action"]["role"], "developer")
        self.assertTrue(current["retryable_action"]["pending_schedule"])

    def test_integration_review_starts_only_a_reviewer_after_dependencies_accept(self):
        plan, nodes, card, monitor, delivered = self._build_complex_plan([_node("n1")])
        integration_worker = next(entry for entry in card.workers
                                  if entry["node_id"] == INTEGRATION_REVIEW_NODE_ID)
        self.assertEqual(integration_worker["role"], "reviewer")
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        self.assertFalse(any(call["node_id"] == INTEGRATION_REVIEW_NODE_ID
                             for call in runner.start_calls))
        for _ in range(2):
            snapshot = monitor.tick(snapshot.run_id, runner)
        calls = [call for call in runner.start_calls
                 if call["node_id"] == INTEGRATION_REVIEW_NODE_ID]
        self.assertEqual([call["role"] for call in calls], ["reviewer"])
        self.assertTrue(calls[0]["read_only"])
        self.assertEqual(calls[0]["allowlist"], [])
        self.assertEqual(snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["developer_generation"], 0)
        self.assertIsNone(snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["developer_identity"])

    def test_completed_integration_evidence_rebinds_after_route_only_reauthorization(self):
        import copy
        plan, nodes, card, monitor, delivered = self._build_complex_plan([_node("n1")])
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [
                ("accepted", {"evidence": dict(CLEARED_INTEGRATION_CLAIM)})],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.status, "complete")
        old_epoch = snapshot.authorization_digest
        changed_nodes = copy.deepcopy(nodes)
        for node in changed_nodes:
            node.contract["adapter_id"] = "cursor"
        record = authorize(build_authorization_card(
            plan, changed_nodes, self.capabilities, engine_evidence_ref=card.engine_evidence_ref
        ), "AUTHORIZE")
        rebound = Monitor(self.paths, plan, changed_nodes).reauthorize(
            snapshot.run_id, record, runner, "executable_contract_changed")
        self.assertNotEqual(old_epoch, rebound.authorization_digest)
        self.assertEqual(rebound.status, "complete")
        self.assertEqual(rebound.integration_review_evidence["authorization_digest"], record.digest)
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).status, "complete")

    def test_same_authorization_with_changed_prd_invalidates_integration(self):
        plan, nodes, card, monitor, delivered = self._build_complex_plan([_node("n1")])
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [
                ("accepted", {"evidence": dict(CLEARED_INTEGRATION_CLAIM)})],
        })
        record = authorize(card, "AUTHORIZE")
        snapshot = monitor.start(record, runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.status, "complete")
        (self.paths.root / "docs" / "prd.md").write_text("new approved requirements\n", encoding="utf-8")
        rebound = monitor.reauthorize(snapshot.run_id, record, runner, "executable_contract_changed")
        self.assertEqual(rebound.nodes["n1"]["status"], "accepted")
        self.assertIsNone(rebound.nodes[INTEGRATION_REVIEW_NODE_ID]["acceptance"])
        self.assertNotEqual(rebound.status, "complete")
        transition = next(event for event in reversed(load_events(self.paths, snapshot.run_id))
                          if event["event"] == "authorization_reauthorized")
        self.assertEqual(transition["data"]["changed_nodes"], [INTEGRATION_REVIEW_NODE_ID])
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).prd_digest, rebound.prd_digest)

    def test_integration_reviewer_write_violation_never_dispatches_developer(self):
        plan, _nodes, card, monitor, delivered = self._build_complex_plan([_node("n1")])
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [
                ("complete", {"reviewer_write_violation": True,
                              "reviewer_modified_files": ["n1.py"]})],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(2):
            snapshot = monitor.tick(snapshot.run_id, runner)
        with patch.object(monitor, "_start_task", wraps=monitor._start_task) as start:
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertFalse(any(call.args[1:3] == (INTEGRATION_REVIEW_NODE_ID, "developer")
                             for call in start.call_args_list))
        self.assertEqual(snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["status"], "blocked_unknown")
        self.assertIsNone(snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["acceptance"])

    def test_integration_preflight_failure_returns_to_planned_without_developer(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        _plan, _nodes, _card, monitor, _delivered = self._build_complex_plan([_node("n1")])
        current = {"review_generation": 1, "reviewer_identity": None, "reviewer_started": True}
        snapshot = SimpleNamespace(nodes={INTEGRATION_REVIEW_NODE_ID: current}, run_id="run-test")
        with patch.object(monitor, "_release_node_lease"), patch.object(monitor, "_record"), patch.object(monitor, "_save_snapshot"):
            monitor._preflight_contract_failure(
                snapshot, INTEGRATION_REVIEW_NODE_ID, "reviewer", "review", False,
                1, "review_generation", ValueError("temporary"), "profile", True,
            )
        self.assertEqual(current["status"], "planned")
        self.assertEqual(current["review_generation"], 0)
        self.assertFalse(current["reviewer_started"])
        self.assertIsNone(current["retryable_action"])

    def test_integration_write_quarantine_cannot_be_cleared_by_reauthorization(self):
        from vibe_guide.state import save_snapshot
        plan, nodes, card, monitor, delivered = self._build_complex_plan([_node("n1")])
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["workspace_reconciliation_required"] = True
        save_snapshot(self.paths, snapshot)
        before = len(load_events(self.paths, snapshot.run_id))
        with self.assertRaisesRegex(PermissionError, "workspace reconciliation"):
            monitor.reauthorize(snapshot.run_id, authorize(card, "AUTHORIZE"), runner,
                                "executable_contract_changed")
        self.assertEqual(len(load_events(self.paths, snapshot.run_id)), before)

    def test_integration_reauthorization_queues_reviewer_not_developer(self):
        current = {"review_generation": 1}
        Monitor._queue_reauthorization_continuation(current, False, "reviewer")
        self.assertEqual(current["retryable_action"]["role"], "reviewer")
        self.assertEqual(current["retryable_action"]["phase"], "review")
        self.assertTrue(current["retryable_action"]["continuation"])

    def test_legacy_delivered_integration_starts_reviewer_without_new_developer(self):
        from vibe_guide.state import save_snapshot
        plan, nodes, card, monitor, delivered = self._build_complex_plan([_node("n1")])
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        original_start = monitor._start_task
        def defer_integration(snapshot, node_id, *args, **kwargs):
            if node_id == INTEGRATION_REVIEW_NODE_ID:
                return False
            return original_start(snapshot, node_id, *args, **kwargs)
        with patch.object(monitor, "_start_task", side_effect=defer_integration):
            for _ in range(2):
                snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["status"] = "delivered"
        save_snapshot(self.paths, snapshot)
        snapshot = monitor.tick(snapshot.run_id, runner)
        calls = [call for call in runner.start_calls
                 if call["node_id"] == INTEGRATION_REVIEW_NODE_ID]
        self.assertEqual([call["role"] for call in calls], ["reviewer"])

    def test_integration_review_finding_expands_target_and_queues_rework(self):
        files = ["tests/test_first.py", "tests/test_second.py"]
        plan, nodes, card, monitor, delivered = self._build_complex_plan(
            [_node("n1")]
        )
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "developer"): [delivered],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [(
                "review_finding",
                {
                    "finding": "two integration fixtures are required",
                    "severity": "P1",
                    "target_node_id": "n1",
                    "scope_expansion": _scope_claim(files),
                },
            )],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(snapshot.nodes["n1"]["scope_expansions"], files)
        self.assertEqual(
            snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["status"], "rework"
        )
        self.assertTrue(
            snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["retryable_action"][
                "pending_schedule"
            ]
        )
        self.assertEqual(
            self._scope_events(snapshot.run_id)[0]["trigger"],
            "integration_rework",
        )

    def test_integration_scope_rework_replays_after_snapshot_write_failure(self):
        plan, nodes, card, monitor, delivered = self._build_complex_plan(
            [_node("n1")]
        )
        runner = _BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "developer"): [delivered],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [(
                "review_finding",
                {
                    "finding": "integration fixture required",
                    "severity": "P1",
                    "target_node_id": "n1",
                    "scope_expansion": _scope_claim(["tests/test_integration.py"]),
                },
            )],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        # Reviewer-only closeout starts one tick earlier: there is no dummy
        # developer delivery to consume before the real review begins.
        for _ in range(2):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["status"], "review"
        )

        with patch("vibe_guide.monitor.save_snapshot", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                monitor.tick(snapshot.run_id, runner)

        recovered = load_snapshot(self.paths, snapshot.run_id)
        monitor._reconcile_unapplied_events(recovered)
        integration = recovered.nodes[INTEGRATION_REVIEW_NODE_ID]
        self.assertEqual(
            recovered.nodes["n1"]["scope_expansions"],
            ["tests/test_integration.py"],
        )
        self.assertEqual(integration["status"], "rework")
        self.assertNotIn(INTEGRATION_REVIEW_NODE_ID, recovered.handles)
        self.assertEqual(integration["retryable_action"]["role"], "reviewer")
        self.assertTrue(integration["retryable_action"]["continuation"])

    def _integration_finding_run(self, inject_handoff):
        class _GenerationBoundRunner(_BoundBindingRunner):
            # A successor needs a task id that differs from its predecessor.
            def task_binding(self, contract, worktree, run_id, status):
                binding = super().task_binding(contract, worktree, run_id, status)
                if contract.get("successor"):
                    binding.task_id = binding.platform_task_id = (
                        binding.task_id + "-g{}".format(contract["generation"])
                    )
                return binding

        plan, nodes, card, monitor, delivered = self._build_complex_plan(
            [_node("n1")]
        )
        runner = _GenerationBoundRunner(events={
            ("n1", "developer"): [delivered, delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [(
                "review_finding",
                {
                    "finding": "integration fixture required",
                    "severity": "P1",
                    "target_node_id": "n1",
                    "scope_expansion": _scope_claim(["tests/test_integration.py"]),
                },
            )],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(2):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["status"], "review")
        old = snapshot.nodes["n1"]["developer_identity"]
        if inject_handoff:
            snapshot.nodes["n1"]["handoff_superseded"] = {"developer": old}
            save_snapshot(self.paths, snapshot)
        for _ in range(3):
            snapshot = monitor.tick(snapshot.run_id, runner)
        starts = [c for c in runner.start_calls
                  if c["node_id"] == "n1" and c["role"] == "developer"]
        return snapshot, starts, old

    def test_integration_finding_rework_returns_to_original_developer_without_handoff(self):
        _snapshot, starts, old = self._integration_finding_run(False)
        self.assertGreaterEqual(len(starts), 2)
        self.assertTrue(starts[-1]["continuation"])
        self.assertFalse(starts[-1]["successor"])
        self.assertEqual(starts[-1]["task_id"], old)

    def test_integration_finding_rework_goes_to_a_successor_after_platform_handoff(self):
        _snapshot, starts, old = self._integration_finding_run(True)
        self.assertGreaterEqual(len(starts), 2)
        self.assertTrue(starts[-1]["successor"])
        self.assertEqual(starts[-1]["predecessor_task_id"], old)
        self.assertNotEqual(starts[-1]["task_id"], old)

    def test_scope_expand_cli_registers_accepted_patch(self):
        self._approve_acceptance_rule()
        node = _node("n1")
        plan = Plan(
            "plan-1", 1, "docs/prd.md", [node.id], "draft",
            decisions=[dict(DECISION, question="canonical name")],
        )
        card = build_authorization_card(plan, [node], self.capabilities)
        monitor = Monitor(self.paths, plan, [node])
        runner = FakeRunner(events={
            ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        plan_dir = self.paths.vibe / "plans" / plan.plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_payload = plan.to_dict()
        plan_payload["status"] = "authorized"
        (plan_dir / "plan.json").write_text(
            json.dumps(plan_payload), encoding="utf-8"
        )
        (plan_dir / "nodes.json").write_text(
            json.dumps([node.to_dict()]), encoding="utf-8"
        )
        (plan_dir / "authorization-card.json").write_text(
            json.dumps(card.to_dict()), encoding="utf-8"
        )

        result = run_cli(
            [
                "scope-expand", "--run-id", snapshot.run_id, "--node", "n1",
                "--payload", json.dumps(
                    _scope_claim(["vibe_guide/protocols/__init__.py"])
                ),
                "--json",
            ],
            self.paths.root,
        )

        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["status"], "recorded")
        self.assertEqual(
            load_snapshot(self.paths, snapshot.run_id).nodes["n1"]["status"],
            "rework",
        )

    def test_scope_expand_cli_keeps_authorization_errors_unknown(self):
        fake_snapshot = SimpleNamespace(run_id="run-1", plan_id="plan-1")
        fake_monitor = Mock()
        fake_monitor.request_scope_expansion.side_effect = PermissionError(
            "snapshot authorization digest is inconsistent"
        )
        with patch("vibe_guide.cli.load_snapshot", return_value=fake_snapshot), patch(
            "vibe_guide.cli._load_plan", return_value=(None, object(), [], None)
        ), patch("vibe_guide.cli.Monitor", return_value=fake_monitor):
            result = run_cli(
                [
                    "scope-expand", "--run-id", "run-1", "--node", "n1",
                    "--payload", json.dumps(_scope_claim(["safe.py"])), "--json",
                ],
                self.paths.root,
            )
        self.assertEqual(result.exit_code, 4)
        self.assertEqual(result.payload["status"], "blocked_unknown")

    def test_scope_expand_cli_maps_only_scope_decision_to_design_block(self):
        fake_snapshot = SimpleNamespace(run_id="run-1", plan_id="plan-1")
        fake_monitor = Mock()
        fake_monitor.request_scope_expansion.side_effect = ScopeDecisionRequired(
            "scope expansion requires product-manager review"
        )
        with patch("vibe_guide.cli.load_snapshot", return_value=fake_snapshot), patch(
            "vibe_guide.cli._load_plan", return_value=(None, object(), [], None)
        ), patch("vibe_guide.cli.Monitor", return_value=fake_monitor):
            result = run_cli(
                [
                    "scope-expand", "--run-id", "run-1", "--node", "n1",
                    "--payload", json.dumps(_scope_claim(["safe.py"])), "--json",
                ],
                self.paths.root,
            )
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.payload["status"], "blocked_design")

    def test_unlisted_file_still_stops_for_the_user(self):
        _monitor, _runner, snapshot = self._run_finding(
            [_node("n1")], ["n1.py", "vibe_guide/protocols/__init__.py"]
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_design")
        self.assertEqual(self._scope_events(snapshot.run_id), [])

    def test_file_held_by_another_active_node_stops_for_the_user(self):
        nodes = [_node("n1"), _node("n2", ["tests/test_shared.py"])]
        _monitor, _runner, snapshot = self._run_finding(
            nodes, ["n1.py", "tests/test_shared.py"]
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_design")
        self.assertEqual(self._scope_events(snapshot.run_id), [])

    def test_expansion_survives_resume_from_events(self):
        # Interrupt right after the events are written and before the
        # snapshot is saved: recovery must rebuild the widened scope from
        # events.jsonl alone and hand it to the rework task.
        with patch.object(
            Monitor,
            "_start_task",
            autospec=True,
            side_effect=_interrupt_rework,
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self._run_finding([_node("n1")], ["n1.py", "tests/test_n1.py"])
        monitor = self._monitor
        recovery = FakeRunner()
        recovered = monitor.resume(self._run_id, recovery)
        self.assertEqual(
            recovered.nodes["n1"]["scope_expansions"], ["tests/test_n1.py"]
        )
        self.assertEqual(recovered.nodes["n1"]["status"], "rework")
        self.assertIn("tests/test_n1.py", recovery.start_calls[-1]["files"])


    def test_integration_review_node_does_not_count_as_a_writer(self):
        # P1-1 end to end: the planned integration reviewer lists every
        # business file; it is read-only and must not block corrections.
        plan, (_monitor, _runner, snapshot) = self._run_complex_finding(
            [_node("n1"), _node("n2")], ["n1.py", "tests/test_n1.py"]
        )
        integration = [n for n in plan.nodes if n.id == INTEGRATION_REVIEW_NODE_ID][0]
        self.assertIn("n1.py", integration.contract["files"])
        self.assertEqual(
            snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["status"], "planned"
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(snapshot.nodes["n1"]["scope_expansions"], ["tests/test_n1.py"])

    def test_integration_review_scope_is_never_counted_as_held(self):
        # P1-1: once n2 is accepted, nothing else writes its files; the
        # planned integration reviewer still lists them but must not hold
        # them (otherwise n1 could not pick up n2's finished test file).
        plan, (monitor, _runner, snapshot) = self._run_complex_finding(
            [_node("n1"), _node("n2", ["tests/test_shared.py"])],
            ["n1.py"],
        )
        integration = [n for n in plan.nodes if n.id == INTEGRATION_REVIEW_NODE_ID][0]
        self.assertIn("tests/test_shared.py", integration.contract["files"])
        self.assertEqual(
            snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]["status"], "planned"
        )
        self.assertIn(
            "tests/test_shared.py",
            monitor._files_held_by_other_active_nodes(snapshot, "n1"),
        )
        snapshot.nodes["n2"]["status"] = "accepted"
        self.assertEqual(monitor._files_held_by_other_active_nodes(snapshot, "n1"), [])

    def test_dormant_downstream_root_allowlist_does_not_hold_scope(self):
        # P1-A ①: no files -> node_spec defaults the allowlist to ["."]
        # (the whole repository is writable), so it holds tests/ too.
        # A dormant downstream reservation has no writer yet and must not
        # deadlock the upstream correction.
        n2 = _node("n2")
        n2.depends_on = ["n1"]
        n2.parallel_group = "g2"
        n2.contract["files"] = []
        n2.contract["worker_profile"]["allowlist"] = ["."]
        _monitor, _runner, snapshot = self._run_finding(
            [_node("n1"), n2], ["n1.py", "tests/test_n1.py"]
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")

    def test_sibling_allowlist_directory_holds_files_beneath_it(self):
        # P1-A ②: the write allowlist names `tests`; files does not.
        n2 = _node("n2")
        n2.contract["worker_profile"]["allowlist"] = ["n2.py", "tests"]
        _monitor, _runner, snapshot = self._run_finding(
            [_node("n1"), n2], ["n1.py", "tests/test_n1.py"]
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_design")

    def test_sibling_owned_paths_hold_files_beneath_them(self):
        n2 = _node("n2")
        n2.contract["owned_paths"] = ["tests/fixtures"]
        _monitor, _runner, snapshot = self._run_finding(
            [_node("n1"), n2], ["n1.py", "tests/fixtures/data.py"]
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_design")

    def test_dormant_downstream_unverifiable_scope_does_not_hold_scope(self):
        # A dormant downstream node still has no writer even when its eventual
        # scope is unverifiable; unknown active writers remain fail closed.
        n2 = _node("n2")
        n2.depends_on = ["n1"]
        n2.parallel_group = "g2"
        n2.contract["worker_profile"]["allowlist"] = []
        _monitor, _runner, snapshot = self._run_finding(
            [_node("n1"), n2], ["n1.py", "tests/test_n1.py"]
        )
        self.assertEqual(snapshot.nodes["n2"]["status"], "planned")
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")

    def test_sibling_with_disjoint_allowlist_does_not_block(self):
        _monitor, _runner, snapshot = self._run_finding(
            [_node("n1"), _node("n2")], ["n1.py", "tests/test_n1.py"]
        )
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")

    def test_scope_rules_survive_persistence_with_sensitive_words_in_paths(self):
        # P2-3: read back the persisted events.jsonl shape.
        files = ["n1.py", "tests/test_token_x.py", "tests/test_secret_y.py"]
        _monitor, _runner, snapshot = self._run_finding([_node("n1")], files)
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        lines = [
            json.loads(line)
            for line in (
                self.paths.vibe / "runs" / snapshot.run_id / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        data = [r["data"] for r in lines if r["event"] == "scope_auto_expanded"][0]
        self.assertEqual(data["files"], files[1:])
        self.assertEqual(
            data["scope_rules"],
            [
                {"path": "tests/test_token_x.py", "rule": "tests_dir"},
                {"path": "tests/test_secret_y.py", "rule": "tests_dir"},
            ],
        )

    def test_status_json_lists_scope_expansions(self):
        # P2-4: the supervisor reads `vibe status --json` to report delivery.
        _monitor, _runner, snapshot = self._run_finding(
            [_node("n1")], ["n1.py", "tests/test_n1.py"]
        )
        result = _snapshot_result("status", load_snapshot(self.paths, snapshot.run_id), True)
        self.assertEqual(
            result.payload["nodes"]["n1"]["scope_expansions"], ["tests/test_n1.py"]
        )


class AgentsSectionSixPatchTests(unittest.TestCase):
    """#157 landed: AGENTS.md §6 separates product scope from file lists.

    The patch is the host rule the ``acceptance_scope_rule_approved`` flag
    depends on (config.py: the flag is "true only after the product manager
    has approved and written the AGENTS.md §6 patch").  Anchoring the text
    keeps the shipped rule and the config flag from drifting apart.
    """

    REPO_ROOT = Path(__file__).resolve().parents[1]

    def _section_six(self):
        text = (self.REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## 6. 监工与授权", text)
        return text.split("## 6. 监工与授权", 1)[1].split("\n## ", 1)[0]

    def test_section_six_carries_the_acceptance_judgment_criterion(self):
        section = self._section_six()
        for fact in (
            "验收例子",  # the judgment-unchanged criterion
            "acceptance_scope_rule_approved",  # the flag, paired with this rule
            "第三方依赖",  # always-human boundary 1
            "后台不可见",  # always-human boundary 2 (visible->background)
            "依赖清单",  # manifests never auto-expand, at any path
            "唯一 writer",  # engineering boundary survives the patch
        ):
            self.assertIn(
                fact, section,
                "#157: AGENTS.md §6 must state {!r}".format(fact),
            )

    def test_section_six_keeps_the_flag_paired_with_the_rule(self):
        section = self._section_six()
        flag_line = next(
            (l for l in section.splitlines() if "acceptance_scope_rule_approved" in l),
            "",
        )
        self.assertTrue(
            flag_line,
            "#157: AGENTS.md §6 must name the acceptance_scope_rule_approved flag",
        )
        self.assertIn(
            "本节规则", flag_line,
            "#157: the flag line must say the switch pairs with this section",
        )

    def test_flag_stays_off_by_default_until_the_project_opts_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            paths.vibe.mkdir(parents=True, exist_ok=True)
            self.assertFalse(
                load_project_config(paths.root).acceptance_scope_rule_approved
            )


if __name__ == "__main__":
    unittest.main()
