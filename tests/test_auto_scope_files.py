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
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.config import load_project_config
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.planner import resolve_consistency
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.cli import _snapshot_result
from vibe_guide.models import INTEGRATION_REVIEW_NODE_ID
from vibe_guide.dag import append_integration_review_node
from vibe_guide.engine_attestation import create_engine_attestation
from vibe_guide.planner import REQUIRED_COMPLEX_WORKFLOW, TaskContext
from vibe_guide.task_registry import TaskBinding
from vibe_guide.workflow_gate import create_task_workflow, record_workflow_node
from vibe_guide.state import load_events, load_snapshot


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

    def _run_finding(self, nodes, files):
        plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft",
            decisions=[dict(DECISION, question="canonical name")],
        )
        card = build_authorization_card(plan, nodes, self.capabilities)
        monitor = Monitor(self.paths, plan, nodes)
        return self._drive(monitor, card, nodes, files)

    def _drive(self, monitor, card, nodes, files, runner_class=FakeRunner,
               delivery=("complete", {"evidence": "delivery"})):
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
        runner.events[("n1", "reviewer")] = [
            (
                "review_finding",
                {
                    "finding": "fixture missing",
                    "in_contract": False,
                    "consistency": _inconsistency(files, binding),
                },
            )
        ]
        snapshot = monitor.tick(snapshot.run_id, runner)
        return monitor, runner, snapshot

    def _run_complex_finding(self, business, files):
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


if __name__ == "__main__":
    unittest.main()
