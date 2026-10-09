"""Regression tests for the gates that blocked a same-run reauthorization.

Every test here reproduces a failure that was observed on a real run rather
than a hypothetical one:

``(C)`` the authorization card advertised ``dual-visible`` for all twelve
nodes while the dispatcher ruled them ``visible-sdd``, because
``refresh_authorization_card`` carried ``previous.workers`` through verbatim.

``(a)`` ``Monitor.reauthorize`` reset every never-started node back to
``planned`` and then dispatched without refreshing the execution projection,
so ``_schedule_ready``'s opening ``_validate_execution_topology`` compared the
new ready set against the stale one and failed closed with ``ready-set
drift``.

``(d)`` ``snapshot.prd_digest``/``spec_digest`` were written once by
``Monitor.start`` and never again, so any post-authorization PRD/Spec edit
made ``resume`` report ``PRD/Spec source drift`` forever.

``(b)`` lived in ``cli.py`` (publish the card only after reauthorize
succeeds) and is covered by ``tests/test_cli.py``.
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from vibe_guide import cli as cli_module
from vibe_guide import monitor as monitor_module
from vibe_guide.authorize_entry import load_live_workflow
from vibe_guide.authorization import (
    BACKGROUND_MODE_DISCLOSURES,
    authorize,
    build_authorization_card,
    dispatch_topology_for_node,
    refresh_authorization_card,
)
from vibe_guide.cli import (
    _VOLATILE_CARD_FIELDS,
    _card_field_diff,
    _engine_evidence_required,
    _load_plan,
    _normalize_card_value,
    _observed_adapter,
    _staged_engine_attestation,
    run_cli,
)
from vibe_guide.engine_attestation import (
    create_engine_attestation,
    validate_engine_attestation,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events, load_snapshot

from tests.support_v45_authorize import publish_complex_probe


def _business_node(node_id, adapter_id="workbuddy", **contract_overrides):
    contract = {
        "files": [node_id + ".py"],
        "worker": "worker-" + node_id,
        "worktree": ".worktrees/" + node_id,
        "adapter_id": adapter_id,
    }
    contract.update(contract_overrides)
    return DAGNode(node_id, node_id, [], [], "g1", contract, "ready")


class DispatchTopologyRulingTests(unittest.TestCase):
    """``(C)`` — one ruling, shared by the card and the dispatcher."""

    def setUp(self):
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def _card(self, nodes):
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        return plan, build_authorization_card(plan, nodes, self.capabilities)

    @staticmethod
    def _by_id(card):
        return {entry["node_id"]: entry for entry in card.workers}

    def test_refresh_rerules_visible_topology_from_the_live_ruling(self):
        nodes = [_business_node("alpha"), _business_node("beta")]
        plan, card = self._card(nodes)
        self.assertEqual(card.topology_summary["by_topology"]["visible-sdd"], 0)

        refreshed = refresh_authorization_card(
            plan, nodes, card, topology_rulings={"workbuddy": "in_session_sdd"}
        )

        entries = self._by_id(refreshed)
        for node_id in ("alpha", "beta"):
            self.assertEqual(entries[node_id]["topology"], "visible-sdd")
            self.assertEqual(entries[node_id]["mode"], "visible")
            self.assertEqual(entries[node_id]["role"], "developer")
        self.assertEqual(refreshed.topology_summary["by_topology"]["visible-sdd"], 2)
        self.assertEqual(refreshed.topology_summary["by_topology"]["dual-visible"], 0)

    def test_refresh_keeps_the_conservative_topology_without_a_ruling(self):
        nodes = [_business_node("alpha")]
        plan, card = self._card(nodes)

        refreshed = refresh_authorization_card(plan, nodes, card, topology_rulings={})

        self.assertEqual(self._by_id(refreshed)["alpha"]["topology"], "dual-visible")

    def test_first_publication_rules_workers_from_the_live_ruling(self):
        """The first card, not only a refresh, must describe the real dispatch.

        ``_normalize_workers_schema`` defaults every undeclared entry to the
        conservative topology, so without a ruling the published card would
        advertise ``dual-visible`` for a node the supervisor dispatches as
        ``visible-sdd`` -- the same split, one publication earlier.
        """
        nodes = [_business_node("alpha"), _business_node("beta")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha", "beta"], "draft")

        without_ruling = build_authorization_card(plan, nodes, self.capabilities)
        self.assertEqual(
            without_ruling.topology_summary["by_topology"]["visible-sdd"], 0
        )

        ruled = build_authorization_card(
            plan,
            nodes,
            self.capabilities,
            topology_rulings={"workbuddy": "in_session_sdd"},
        )

        entries = self._by_id(ruled)
        for node_id in ("alpha", "beta"):
            self.assertEqual(entries[node_id]["topology"], "visible-sdd")
            self.assertEqual(entries[node_id]["mode"], "visible")
            self.assertEqual(entries[node_id]["role"], "developer")
        self.assertEqual(ruled.topology_summary["by_topology"]["visible-sdd"], 2)

    def test_first_publication_stays_conservative_without_a_ruling(self):
        nodes = [_business_node("alpha")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")

        card = build_authorization_card(
            plan, nodes, self.capabilities, topology_rulings={}
        )

        self.assertEqual(self._by_id(card)["alpha"]["topology"], "dual-visible")

    def test_refresh_never_silently_upgrades_a_disclosed_background_worker(self):
        nodes = [_business_node("alpha")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")
        card = build_authorization_card(
            plan,
            nodes,
            self.capabilities,
            workers={
                "alpha": {
                    "mode": "background",
                    "limitations": list(BACKGROUND_MODE_DISCLOSURES),
                }
            },
        )

        refreshed = refresh_authorization_card(
            plan, nodes, card, topology_rulings={"workbuddy": "in_session_sdd"}
        )

        entry = self._by_id(refreshed)["alpha"]
        self.assertEqual(entry["topology"], "background")
        self.assertEqual(entry["mode"], "background")
        self.assertEqual(entry["limitations"], tuple(BACKGROUND_MODE_DISCLOSURES))

    def test_integration_review_is_never_ruled_into_the_single_session_topology(self):
        """The closeout node keeps its reviewer-role dispatch under any ruling.

        Removing this pin was tried and measured (2026-10-07).  With the pin
        gone the monitor dispatched ``integration-review`` with
        ``role=developer`` like any other node; the run-level
        ``integration_review_evidence`` package is only ever derived from a
        *reviewer* acceptance claim (``monitor._derive_integration_acceptance``),
        so it stayed empty and the run stalled on ``integration review evidence
        is missing`` instead of reaching ``complete``.  With the pin in place the
        node is dispatched twice -- once through the ruling, once as a reviewer
        -- and the second dispatch is what closes the run out.
        ``tests/test_integration_review_closeout_entry.py`` pins the same
        behaviour end to end.
        """
        node = _business_node("integration-review")

        self.assertEqual(
            dispatch_topology_for_node(node, {"workbuddy": "in_session_sdd"}),
            "dual-visible",
        )
        # A plan that genuinely needs the two-visible-session shape states it in
        # the node contract, which still wins over the ruling.
        self.assertEqual(
            dispatch_topology_for_node(
                _business_node("integration-review", dispatch_topology="dual-visible"),
                {"workbuddy": "in_session_sdd"},
            ),
            "dual-visible",
        )

    def test_persisted_contract_ruling_wins_over_the_live_ruling(self):
        node = _business_node("alpha", dispatch_topology="dual-visible")

        self.assertEqual(
            dispatch_topology_for_node(node, {"workbuddy": "in_session_sdd"}),
            "dual-visible",
        )

    def test_unknown_ruling_never_upgrades_to_visible_sdd(self):
        node = _business_node("alpha")

        for ruling in (None, "", "unknown", "visible", "dual-visible"):
            with self.subTest(ruling=ruling):
                self.assertEqual(
                    dispatch_topology_for_node(node, {"workbuddy": ruling}),
                    "dual-visible",
                )

    def test_card_ruling_matches_the_dispatcher_ruling(self):
        cases = (
            ("in_session_sdd", "visible-sdd"),
            ("dual-visible", "dual-visible"),
            ("unknown", "dual-visible"),
            ("", "dual-visible"),
        )
        for ruling, expected in cases:
            with self.subTest(ruling=ruling):
                nodes = [_business_node("alpha"), _business_node("beta")]
                plan, card = self._card(nodes)
                rulings = {"workbuddy": ruling}
                refreshed = refresh_authorization_card(
                    plan, nodes, card, topology_rulings=rulings
                )
                dispatcher = object.__new__(Monitor)
                dispatcher._topology_rulings = dict(rulings)

                entries = self._by_id(refreshed)
                for node in nodes:
                    self.assertEqual(
                        entries[node.id]["topology"],
                        dispatcher._node_dispatch_topology(node),
                        "card and dispatcher disagree for " + node.id,
                    )
                self.assertEqual(entries["alpha"]["topology"], expected)

    def test_dispatcher_and_card_agree_on_the_integration_review_closeout_node(self):
        node = _business_node("integration-review")
        rulings = {"workbuddy": "in_session_sdd"}
        dispatcher = object.__new__(Monitor)
        dispatcher._topology_rulings = dict(rulings)

        self.assertEqual(
            dispatcher._node_dispatch_topology(node),
            dispatch_topology_for_node(node, rulings),
        )


class ReauthorizeComplexProjectionTests(unittest.TestCase):
    """``(a)`` and ``(d)`` — observed on a real complex run."""

    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, self.plan, self.nodes, self.card = _load_plan(
            self.paths, "probe-plan"
        )
        self.attestation = json.loads(
            (self.directory / "engine-attestation.json").read_text(encoding="utf-8")
        )
        # Park the only business node at the brief gate: that is how a real run
        # reaches reauthorization with `developer_generation == 0` and an empty
        # recorded ready set, which is the state the transition then moves.
        self._apply_brief_gate(self.nodes)
        record = authorize(self._recard(self.plan, self.nodes), "AUTHORIZE")
        self.snapshot = Monitor(self.paths, self.plan, self.nodes).start(
            record, FakeRunner()
        )
        self.assertEqual(self.snapshot.nodes["probe-node-a"]["status"], "brief_pending")
        self.assertEqual(self.snapshot.nodes["probe-node-a"]["developer_generation"], 0)
        self.assertEqual(list(self.snapshot.ready_set), [])
        # The CLI flips plan.json to `authorized` after a successful monitor
        # run; a refresh is only reachable on such a plan.
        published = json.loads((self.directory / "plan.json").read_text(encoding="utf-8"))
        published["status"] = "authorized"
        (self.directory / "plan.json").write_text(
            json.dumps(published), encoding="utf-8"
        )

    @staticmethod
    def _apply_brief_gate(nodes):
        for node in nodes:
            if node.id == "probe-node-a":
                node.contract["brief_required"] = True
                # Deliberately missing `base_sha`: the gate holds the node
                # before any writer lease or provider start.
                node.contract["implementation_brief"] = {
                    "issue_id": node.id,
                    "goal": "goal",
                    "non_goals": [],
                    "owned_paths": [node.id + ".py"],
                    "read_paths": [],
                    "call_chain": [node.id + ".py:missing"],
                    "invariants": [
                        {
                            "id": "I1",
                            "entrypoint": node.id + ".py:missing",
                            "positive_case": "ok",
                            "negative_case": "bad",
                            "test_command": "python -m unittest",
                        }
                    ],
                    "plan_revision": 1,
                    "execution_epoch": 0,
                    "evidence_ref": "brief.json",
                }

    def _recard(self, plan, nodes):
        """Rebuild the authorized card for a mutated node set.

        The on-disk engine attestation is carried through explicitly: a complex
        plan still in ``confirmed_pending_authorization`` refuses a card
        without it.
        """
        return build_authorization_card(
            plan,
            nodes,
            AgentCapabilities(self.card.agent_id, False, False, False, False, False, "guide"),
            active_pair_limit=self.card.active_pair_limit,
            allowed_actions=self.card.allowed_actions,
            remote_git_actions=self.card.remote_git_actions,
            required_workflow=self.card.required_workflow,
            skipped_nodes=self.card.skipped_nodes,
            integration_node_id=self.card.integration_node_id,
            integration_review_scope=self.card.integration_review_scope,
            workflow=load_live_workflow(self.paths, plan.plan_id),
            execution_engine=self.card.execution_engine,
            engine_mode=self.card.engine_mode,
            engine_evidence_ref=self.card.engine_evidence_ref,
            engine_attestation=self.attestation,
            explicit_execution_mode_override=self.card.explicit_execution_mode_override,
            workers=self.card.workers,
        )

    def _rebound(self):
        """Return ``(monitor, record)`` after a contract change forces a re-pin."""
        _, plan, nodes, _card = _load_plan(self.paths, "probe-plan")
        self._apply_brief_gate(nodes)
        for node in nodes:
            if node.id == "probe-node-a":
                node.contract["acceptance_example"] = "verified_fact: README 含两行文本"
        return Monitor(self.paths, plan, nodes), authorize(
            self._recard(plan, nodes), "AUTHORIZE"
        )

    def test_reauthorize_dispatches_after_refreshing_the_stale_ready_set(self):
        monitor, record = self._rebound()
        self.assertEqual(list(self.snapshot.ready_set), [])

        rebound = monitor.reauthorize(
            self.snapshot.run_id, record, FakeRunner(), "executable_contract_changed"
        )

        self.assertEqual(rebound.authorization_digest, record.digest)
        # The transition moved the ready set from empty to the reset node; the
        # projection must have been re-recorded for the validator to accept it.
        names = [event["event"] for event in load_events(self.paths, rebound.run_id)]
        transition = names.index("authorization_reauthorized")
        self.assertIn("execution_topology_observed", names[transition + 1 :])
        # A follow-up tick re-runs the same validator against the persisted
        # snapshot, so a stale projection cannot survive here either.
        monitor.tick(rebound.run_id, FakeRunner())

    def test_without_the_projection_refresh_reauthorize_fails_closed(self):
        """Negative control: removing the refresh reproduces the original block."""
        monitor, record = self._rebound()
        original = Monitor._record_topology_projection

        def only_other_entries(inner_self, snapshot, entry):
            if entry == "monitor.reauthorize":
                return
            return original(inner_self, snapshot, entry)

        with patch.object(Monitor, "_record_topology_projection", only_other_entries):
            with self.assertRaisesRegex(
                PermissionError, "ready-set drift"
            ):
                monitor.reauthorize(
                    self.snapshot.run_id,
                    record,
                    FakeRunner(),
                    "executable_contract_changed",
                )

    def test_reauthorize_repins_an_edited_prd_and_resume_recovers(self):
        prd = self.root / ".vibe" / "plans" / "probe-plan" / "prd.md"
        prd.write_text(
            prd.read_text(encoding="utf-8") + "\n\n新增一段说明。\n", encoding="utf-8"
        )
        new_digest = hashlib.sha256(prd.read_bytes()).hexdigest()
        self.assertNotEqual(new_digest, self.snapshot.prd_digest)

        # The legal re-materialization entry has to run first: with stale
        # workflow evidence `resume` refuses before it reaches lineage at all.
        rematerialized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        self.assertEqual(rematerialized.payload.get("status"), "ok")
        with self.assertRaisesRegex(PermissionError, "PRD/Spec source drift"):
            Monitor(self.paths, self.plan, self.nodes).resume(
                self.snapshot.run_id, FakeRunner()
            )

        monitor, record = self._rebound()
        rebound = monitor.reauthorize(
            self.snapshot.run_id, record, FakeRunner(), "executable_contract_changed"
        )

        self.assertEqual(rebound.prd_digest, new_digest)
        self.assertEqual(rebound.spec_digest, new_digest)
        transition = next(
            event
            for event in load_events(self.paths, rebound.run_id)
            if event["event"] == "authorization_reauthorized"
        )
        self.assertEqual(
            transition["data"]["previous_prd_digest"], self.snapshot.prd_digest
        )
        self.assertEqual(transition["data"]["prd_digest"], new_digest)
        # The durable snapshot, not just the in-memory one, carries the re-pin.
        self.assertEqual(
            load_snapshot(self.paths, rebound.run_id).prd_digest, new_digest
        )

        monitor.resume(rebound.run_id, FakeRunner())


class FailedReauthorizationCardPublicationTests(unittest.TestCase):
    """``(b)`` — the card may only be published once reauthorize succeeded."""

    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, plan, nodes, card = _load_plan(self.paths, "probe-plan")
        record = authorize(card, "AUTHORIZE")
        self.snapshot = Monitor(self.paths, plan, nodes).start(record, FakeRunner())
        # Mirror the state a successful monitor run leaves behind, so the
        # `elif _current_run_path(directory).exists()` branch is selected.
        published = json.loads((self.directory / "plan.json").read_text(encoding="utf-8"))
        published["status"] = "authorized"
        (self.directory / "plan.json").write_text(
            json.dumps(published), encoding="utf-8"
        )
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": self.snapshot.run_id}), encoding="utf-8"
        )
        # A delivered contract correction, so the refreshed card digest moves
        # and a premature write would be observable.
        nodes_path = self.directory / "nodes.json"
        payload = json.loads(nodes_path.read_text(encoding="utf-8"))
        target = payload["nodes"][0] if isinstance(payload, dict) else payload[0]
        target["contract"]["acceptance_example"] = "verified_fact: README 含两行文本"
        nodes_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        self.card_path = self.directory / "authorization-card.json"

    def test_failed_reauthorization_leaves_the_published_card_untouched(self):
        before = self.card_path.read_bytes()
        # Non-vacuousness: the refresh this run would have published really is
        # a different card, so leaving the file alone is a real assertion.
        _, plan, nodes, card = _load_plan(self.paths, "probe-plan")
        refreshed = refresh_authorization_card(
            plan, nodes, card, workflow=load_live_workflow(self.paths, plan.plan_id)
        )
        self.assertNotEqual(refreshed.digest, card.digest)

        def explode(*_args, **_kwargs):
            raise RuntimeError("reauthorize refused by test")

        with patch.object(Monitor, "reauthorize", explode):
            result = run_cli(
                ["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
                self.root,
            )

        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertEqual(self.card_path.read_bytes(), before)


class EngineEvidenceStagingTests(unittest.TestCase):
    """The evidence a reauthorization names must be on disk before it runs.

    ``Monitor._require_record`` re-reads ``engine-attestation.json``, while the
    refreshed card is deliberately published only after ``reauthorize``
    succeeds.  Those two orderings cannot both be "after", so the evidence is
    staged for the duration of the call and put back when it raises.  A
    reauthorization that fails must leave the plan exactly as it found it.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vg-staged-attestation-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "engine-attestation.json"

    def test_a_raise_restores_the_previous_bytes(self):
        original = b'{"previous": true}'
        self.path.write_bytes(original)

        with self.assertRaises(RuntimeError):
            with _staged_engine_attestation(self.path, {"fresh": True}):
                self.assertEqual(
                    json.loads(self.path.read_text(encoding="utf-8")), {"fresh": True}
                )
                raise RuntimeError("reauthorize refused by test")

        self.assertEqual(self.path.read_bytes(), original)

    def test_a_raise_removes_a_file_that_did_not_exist_before(self):
        with self.assertRaises(RuntimeError):
            with _staged_engine_attestation(self.path, {"fresh": True}):
                raise RuntimeError("reauthorize refused by test")

        self.assertFalse(self.path.exists())

    def test_a_success_leaves_the_staged_evidence_in_place(self):
        self.path.write_bytes(b'{"previous": true}')

        with _staged_engine_attestation(self.path, {"fresh": True}) as staged:
            self.assertEqual(staged, {"fresh": True})

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")), {"fresh": True}
        )


class _StubCard:
    """Stand-in carrying only the serialization shape ``_card_field_diff`` reads."""

    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


class CardDiffRepresentationTests(unittest.TestCase):
    """A diff between a loaded card and a derived one must be a real diff.

    ``AuthorizationCard.to_dict()`` hands back tuples; the same card read from
    ``authorization-card.json`` carries lists.  Comparing them raw reported
    ``integration_review_scope``, ``required_workflow`` and ``skipped_nodes``
    as changed on every preview and buried the fields a reviewer has to read.
    """

    def setUp(self):
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def _card(self):
        nodes = [_business_node("alpha")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")
        return build_authorization_card(plan, nodes, self.capabilities)

    def test_a_list_and_a_tuple_representation_of_one_card_are_not_a_diff(self):
        derived = self._card().to_dict()
        reloaded = json.loads(json.dumps(derived, ensure_ascii=False))

        self.assertEqual(
            _card_field_diff(_StubCard(reloaded), _StubCard(derived)), {}
        )

    def test_a_real_change_is_still_reported(self):
        card = self._card()
        other = build_authorization_card(
            Plan("plan-1", 2, "docs/prd.md", ["alpha"], "draft"),
            [_business_node("alpha")],
            self.capabilities,
        )

        changed = _card_field_diff(card, other)

        self.assertIn("plan_version", changed)
        self.assertNotIn("required_workflow", changed)


class RefreshNamesFreshEngineEvidenceTests(unittest.TestCase):
    """``refresh_authorization_card`` must be able to re-sign new evidence.

    Run on the published probe plan rather than a hand-built card: a complex
    plan refuses a card that does not carry the complete required workflow and
    the plan's own integration contract, so a hand-built stand-in would test
    the stand-in.
    """

    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        _directory, self.plan, self.nodes, self.card = _load_plan(self.paths, "probe-plan")
        # A refresh that names no new evidence is only reachable once the plan
        # is past publication confirmation; before that the card build refuses
        # a complex plan without a verified attestation.
        self.plan.status = "authorized"

    def _attestation(self, now):
        return create_engine_attestation(
            self.plan.plan_id, self.plan.version, "vibeguide_monitor", "dag",
            self.card.agent_id, {self.card.agent_id + ".subprocess": True}, "test", now,
        )

    @staticmethod
    def _fresh_timestamp():
        """A timestamp that is fresh whenever the suite runs, not just today.

        ``validate_engine_attestation`` refuses evidence older than a day, so a
        literal such as ``2026-10-07T00:00:00Z`` makes these tests expire at the
        next midnight: they passed on the day they were written and failed from
        the following day on, which is indistinguishable from a real regression
        until you notice the clock.
        """
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _refresh(self, **kwargs):
        # A complex plan refuses a refresh without the workflow evidence the
        # published card preserved.
        return refresh_authorization_card(
            self.plan,
            self.nodes,
            self.card,
            workflow=load_live_workflow(self.paths, self.plan.plan_id),
            **kwargs
        )

    def test_without_new_evidence_the_previous_reference_is_carried(self):
        refreshed = self._refresh()

        self.assertEqual(
            refreshed.engine_evidence_ref, self.card.engine_evidence_ref
        )

    def test_new_evidence_replaces_the_carried_reference(self):
        fresh = self._attestation(self._fresh_timestamp())
        self.assertNotEqual(fresh["evidence_ref"], self.card.engine_evidence_ref)

        refreshed = self._refresh(engine_attestation=fresh)

        self.assertEqual(refreshed.engine_evidence_ref, fresh["evidence_ref"])
        self.assertEqual(refreshed.engine_authorization_digest, refreshed.digest)

    def test_the_carried_reference_is_not_re_passed_beside_new_evidence(self):
        """Both at once is a contradiction, and must not be silently resolved.

        ``build_authorization_card`` refuses a reference that disagrees with
        the attestation it was handed, so the refresh clears the carried
        reference whenever it has fresh evidence to name instead.  Passing
        both is what the refresh must not do.
        """
        fresh = self._attestation(self._fresh_timestamp())

        with self.assertRaisesRegex(ValueError, "does not match attestation"):
            build_authorization_card(
                self.plan, self.nodes, self._capabilities(),
                engine_evidence_ref=self.card.engine_evidence_ref,
                engine_attestation=fresh,
                required_workflow=self.card.required_workflow,
                integration_node_id=self.card.integration_node_id,
                integration_review_scope=self.card.integration_review_scope,
                workflow=load_live_workflow(self.paths, self.plan.plan_id),
                execution_engine=self.card.execution_engine,
                engine_mode=self.card.engine_mode,
                active_pair_limit=self.card.active_pair_limit,
                allowed_actions=self.card.allowed_actions,
                remote_git_actions=self.card.remote_git_actions,
            )

    def _capabilities(self):
        return AgentCapabilities(
            self.card.agent_id, False, False, False, False, False, "guide"
        )


class ExpiredEngineEvidenceReauthorizationTests(unittest.TestCase):
    """A plan older than the evidence window must still be reauthorizable.

    ``validate_engine_attestation`` refuses evidence older than a day, and the
    only writer of ``engine-attestation.json`` is publication, which refuses to
    run for a plan that already exists.  A plan published yesterday therefore
    had no supported way back: the refreshed card kept naming the expired
    reference and ``Monitor._require_record`` blocked before any dispatch was
    considered.

    Time is what ages the evidence, so time is what the test moves:
    ``_aged_evidence`` makes the validator see the already-published
    attestation as a day past its window, and nothing else.
    """

    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, self.plan, nodes, card = _load_plan(self.paths, "probe-plan")
        self.attestation_path = self.directory / "engine-attestation.json"
        self.published = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        self.card_path = self.directory / "authorization-card.json"
        self.card_before = json.loads(self.card_path.read_text(encoding="utf-8"))
        self.snapshot = Monitor(self.paths, self.plan, nodes).start(
            authorize(card, "AUTHORIZE"), FakeRunner()
        )
        # A delivered contract correction, so the refreshed card really is a
        # different card and the assertions below are not vacuous.  It has to
        # land after ``start``: the run verifies the published workflow
        # evidence against ``nodes.json`` and would refuse a node set that no
        # longer matches what was recorded.
        self._edit_a_node_contract()
        # Mirror the state a successful monitor run leaves behind, so the
        # `elif _current_run_path(directory).exists()` branch is selected.
        published = json.loads((self.directory / "plan.json").read_text(encoding="utf-8"))
        published["status"] = "authorized"
        (self.directory / "plan.json").write_text(json.dumps(published), encoding="utf-8")
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": self.snapshot.run_id}), encoding="utf-8"
        )

    def _edit_a_node_contract(self):
        nodes_path = self.directory / "nodes.json"
        payload = json.loads(nodes_path.read_text(encoding="utf-8"))
        target = payload["nodes"][0] if isinstance(payload, dict) else payload[0]
        target["contract"]["acceptance_example"] = "verified_fact: README 含两行文本"
        nodes_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _aged_evidence(self):
        real = monitor_module.validate_engine_attestation
        beyond = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat().replace(
            "+00:00", "Z"
        )

        def validate(candidate, plan_id, plan_revision, **kwargs):
            if candidate.get("digest") == self.published["digest"]:
                kwargs["now"] = beyond
            return real(candidate, plan_id, plan_revision, **kwargs)

        return patch.object(monitor_module, "validate_engine_attestation", validate)

    def _reauthorize(self):
        return run_cli(
            ["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )

    def test_the_published_evidence_is_what_expires(self):
        """Non-vacuousness: the fixture really is past the window."""
        with self._aged_evidence():
            with self.assertRaisesRegex(ValueError, "expired"):
                monitor_module.validate_engine_attestation(
                    self.published, "probe-plan", self.plan.version
                )

    def test_reauthorization_replaces_the_expired_evidence(self):
        with self._aged_evidence():
            self._reauthorize()

        # The run's own event log is the signal: a refused reauthorization
        # leaves no transition behind, whatever status the snapshot reports.
        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertIn("authorization_reauthorized", names)
        refreshed = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        self.assertNotEqual(refreshed["digest"], self.published["digest"])
        # The replacement is real evidence, not a re-typed copy: it validates
        # against the plan binding with the clock left alone.
        validate_engine_attestation(refreshed, "probe-plan", self.plan.version)
        # The card names the evidence that is actually on disk.
        card = json.loads(self.card_path.read_text(encoding="utf-8"))
        self.assertEqual(card["engine_evidence_ref"], refreshed["evidence_ref"])
        self.assertNotEqual(card["digest"], self.card_before["digest"])

    def test_carrying_the_expired_reference_forward_still_fails_closed(self):
        """Negative control: the pre-fix behaviour is still a refusal.

        Returning the already-published attestation is exactly what the old
        path did by carrying ``previous.engine_evidence_ref`` through, so this
        is the block the fix removes -- not a block it merely renames.
        """
        before = self.card_path.read_bytes()
        with self._aged_evidence():
            with patch.object(
                cli_module,
                "_observed_engine_attestation",
                lambda _paths, _plan: dict(self.published),
            ):
                result = self._reauthorize()

        self.assertIn("execution_engine_unverified", result.payload.get("reason", ""))
        self.assertEqual(self.card_path.read_bytes(), before)
        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertNotIn("authorization_reauthorized", names)

    def test_reauthorization_refreshes_workflow_before_public_resume(self):
        state_path = self.paths.vibe / "state.json"
        before = json.loads(state_path.read_text(encoding="utf-8"))
        before["task_workflow"]["other-plan"] = {"preserved": True}
        state_path.write_text(json.dumps(before), encoding="utf-8")
        with self._aged_evidence():
            self._reauthorize()

        card = json.loads(self.card_path.read_text(encoding="utf-8"))
        workflow = load_live_workflow(self.paths, "probe-plan")
        self.assertEqual(
            workflow["node_records"]["plan_confirmation"]["output"]["authorization_digest"],
            card["digest"],
        )
        after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(after["task_workflow"]["other-plan"], {"preserved": True})
        for key in set(before) - {"task_workflow"}:
            self.assertEqual(after[key], before[key])
        resumed = run_cli(
            ["resume", "--plan", "probe-plan", "--run-id", self.snapshot.run_id, "--json"],
            self.root,
        )
        self.assertNotIn("workflow_evidence_stale", resumed.payload.get("reason", ""))
        self.assertNotEqual(resumed.payload.get("status"), "blocked_design")
        current = load_snapshot(self.paths, self.snapshot.run_id)
        self.assertEqual(current.run_id, self.snapshot.run_id)
        self.assertEqual(current.authorization_digest, card["digest"])

    def test_workflow_refresh_failure_retains_invalidation_and_can_retry(self):
        invalidation = self.directory / "authorization-invalidated.json"
        invalidation.write_text(json.dumps({"reason": "contract correction"}), encoding="utf-8")
        before = (self.paths.vibe / "state.json").read_bytes()
        with patch.object(
            cli_module, "materialize_workflow_evidence",
            side_effect=OSError("workflow publication refused by test"),
        ):
            result = self._reauthorize()
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertTrue(invalidation.exists())
        self.assertEqual((self.paths.vibe / "state.json").read_bytes(), before)
        self._reauthorize()
        self.assertFalse(invalidation.exists())
        current = load_snapshot(self.paths, self.snapshot.run_id)
        self.assertEqual(current.run_id, self.snapshot.run_id)
        resumed = run_cli(
            ["resume", "--plan", "probe-plan", "--run-id", self.snapshot.run_id, "--json"],
            self.root,
        )
        self.assertNotIn("workflow_evidence_stale", resumed.payload.get("reason", ""))

    def test_a_plan_without_an_engine_binding_stages_nothing(self):
        """Non-complex plans must not acquire evidence they cannot carry.

        The predicate is stubbed so the real call site runs with
        ``engine_attestation=None``; the predicate itself is covered for every
        band in ``EngineEvidenceScopeTests``.
        """
        before = self.attestation_path.read_bytes()

        with patch.object(cli_module, "_engine_evidence_required", lambda _plan: False):
            self._reauthorize()

        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertIn("authorization_reauthorized", names)
        self.assertEqual(self.attestation_path.read_bytes(), before)

    def test_a_failed_card_publication_restores_the_evidence(self):
        """The staged window must cover the card write, not stop before it.

        With the card published after the window closed, a failing write left
        freshly observed evidence on disk beside a card still naming the old
        reference -- the exact pairing the staging exists to prevent.
        """
        before_evidence = self.attestation_path.read_bytes()
        before_card = self.card_path.read_bytes()
        real = cli_module._atomic_json

        def explode_on_the_card(path, payload):
            if Path(path).name == "authorization-card.json":
                raise OSError("card publication refused by test")
            return real(path, payload)

        with patch.object(cli_module, "_atomic_json", explode_on_the_card):
            result = self._reauthorize()

        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertEqual(self.attestation_path.read_bytes(), before_evidence)
        self.assertEqual(self.card_path.read_bytes(), before_card)

    def test_a_refused_reauthorization_leaves_the_published_card_alone(self):
        before = self.card_path.read_bytes()

        def explode(*_args, **_kwargs):
            raise RuntimeError("reauthorize refused by test")

        with self._aged_evidence():
            with patch.object(Monitor, "reauthorize", explode):
                result = self._reauthorize()

        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertEqual(self.card_path.read_bytes(), before)
        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertNotIn("authorization_reauthorized", names)


class EngineEvidenceScopeTests(unittest.TestCase):
    """Only a complex plan carries an engine binding, so only it may be staged.

    Observing evidence for a simple or light plan would hard-require a
    capability store that non-complex publication never reads -- turning a
    reauthorization that used to work into ``retry_pending`` -- and would leave
    behind an ``engine-attestation.json`` the plan is not allowed to carry.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vg-engine-scope-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "engine-attestation.json"

    @staticmethod
    def _plan(band):
        return Plan("p", 1, "prd.md", [], "draft", complexity_band=band)

    def test_only_a_complex_plan_requires_engine_evidence(self):
        for band in ("simple", "light_plan", ""):
            with self.subTest(band=band):
                self.assertFalse(_engine_evidence_required(self._plan(band)))
        self.assertTrue(_engine_evidence_required(self._plan("complex")))

    def test_staging_none_creates_nothing(self):
        with _staged_engine_attestation(self.path, None) as staged:
            self.assertIsNone(staged)
            self.assertFalse(self.path.exists())

        self.assertFalse(self.path.exists())

    def test_staging_none_leaves_an_existing_file_untouched(self):
        original = b'{"previous": true}'
        self.path.write_bytes(original)

        with _staged_engine_attestation(self.path, None) as staged:
            self.assertIsNone(staged)

        self.assertEqual(self.path.read_bytes(), original)


class CardPreviewTests(unittest.TestCase):
    """``monitor --preview-card`` is the read-only half of the approval loop.

    Publishing the refreshed card on its own is not a representable state: the
    execution gate binds the on-disk card to the run snapshot's authorized
    digest, so a refreshed card with the previous authorization beside it fails
    closed with ``plan-confirmation.invalid``.  The preview derives the same
    card in memory instead, which is the only way to show a reviewer what a
    reauthorization would sign before it is signed.
    """

    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, plan, nodes, card = _load_plan(self.paths, "probe-plan")
        self.snapshot = Monitor(self.paths, plan, nodes).start(
            authorize(card, "AUTHORIZE"), FakeRunner()
        )
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": self.snapshot.run_id}), encoding="utf-8"
        )
        # A delivered contract correction, so the preview has something real to
        # report rather than an empty diff.  It has to land after ``start``:
        # the run verifies the published workflow evidence against
        # ``nodes.json`` and would refuse a node set that no longer matches.
        nodes_path = self.directory / "nodes.json"
        payload = json.loads(nodes_path.read_text(encoding="utf-8"))
        target = payload["nodes"][0] if isinstance(payload, dict) else payload[0]
        target["contract"]["acceptance_example"] = "verified_fact: README 含两行文本"
        nodes_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # The CLI flips plan.json to `authorized` after a successful monitor
        # run; a refresh is only reachable on such a plan.
        published = json.loads((self.directory / "plan.json").read_text(encoding="utf-8"))
        published["status"] = "authorized"
        (self.directory / "plan.json").write_text(json.dumps(published), encoding="utf-8")

    def _fingerprint(self):
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def _preview(self, extra=()):
        return run_cli(
            ["monitor", "--plan", "probe-plan", "--preview-card", "--json", *extra],
            self.root,
        )

    def test_the_preview_writes_nothing(self):
        before = self._fingerprint()

        result = self._preview()

        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(self._fingerprint(), before)

    def test_the_preview_needs_no_authorize_token(self):
        """Reading a card must not require the token that signs it."""
        result = self._preview()

        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(result.payload["publication"], "same_run_reauthorization")

    def test_the_preview_reports_a_missing_run_instead_of_raising(self):
        """The preview sits outside the monitor command's own handler.

        A dangling ``current-run.json`` used to raise straight out of the CLI:
        the call is made before the ``monitor`` try block, so nothing catches
        it.  Reading a card must never be the thing that crashes the process.
        """
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": "run-that-does-not-exist"}), encoding="utf-8"
        )
        before = self._fingerprint()

        result = self._preview()

        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertTrue(result.payload.get("reason"))
        self.assertEqual(self._fingerprint(), before)

    def test_the_preview_discloses_that_its_digest_will_change(self):
        """A ``--json`` consumer must not read the preview digest as a credential."""
        result = self._preview()

        self.assertFalse(result.payload["preview_card_digest_matches_publication"])
        self.assertNotEqual(
            result.payload["preview_card_digest"],
            json.loads(
                (self.directory / "authorization-card.json").read_text(encoding="utf-8")
            )["digest"],
        )

    def test_the_preview_reports_the_fields_the_reauthorization_will_change(self):
        result = self._preview()

        changed = result.payload["changed_fields"]
        # The reviewer-facing diff is against the card that was approved, so
        # the contract correction shows up rather than only the engine fields.
        self.assertIn("node_contract_digest", changed)
        self.assertIn("digest", changed)
        self.assertEqual(
            changed["engine_evidence_ref"]["previous"],
            json.loads(
                (self.directory / "authorization-card.json").read_text(encoding="utf-8")
            )["engine_evidence_ref"],
        )
        # Representation-only fields must not appear: a tuple on one side and a
        # list on the other is not a change a reviewer has to consider.
        for noisy in ("required_workflow", "skipped_nodes", "integration_review_scope"):
            self.assertNotIn(noisy, changed)
        # The narrower self-check -- a refresh with old evidence against a
        # refresh with new evidence -- must stay clean.
        self.assertEqual(result.payload["engine_refresh_drift"], [])

    def test_the_preview_reports_a_topology_move(self):
        """The fixture rules ``dual-visible``; a passing probe must move it.

        The card-level half of the preview's job: the business node follows the
        new ruling, while the closeout node keeps the reviewer-role dispatch
        that rule 2 of ``dispatch_topology_for_node`` reserves for it -- the run
        cannot close out without it.
        """
        self._probe_in_session_sdd()

        changed = self._preview().payload["changed_fields"]

        self.assertIn("workers", changed)
        self.assertIn("topology_summary", changed)
        workers = changed["workers"]["next"]
        self.assertEqual(
            {entry["node_id"]: entry["topology"] for entry in workers},
            {"probe-node-a": "visible-sdd", "integration-review": "dual-visible"},
        )

    def _probe_in_session_sdd(self):
        store = self.root / ".vibe" / "provider-actions" / "capabilities.json"
        payload = json.loads(store.read_text(encoding="utf-8"))
        payload["facts"]["claude-code.in_session_sdd"] = True
        store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_an_underivable_self_check_is_unknown_rather_than_clean(self):
        """``unknown`` must never be reported as "no drift found".

        A plan whose lifecycle never advanced past
        ``confirmed_pending_authorization`` has a run but no refresh that names
        the old evidence, so the drift comparison has nothing to compare
        against.  The card is still shown -- that is the point of a read-only
        preview -- and the missing check is disclosed.
        """
        plan_path = self.directory / "plan.json"
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        payload["status"] = "confirmed_pending_authorization"
        plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        result = self._preview()

        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertIsNone(result.payload["engine_refresh_drift"])
        self.assertTrue(result.payload["engine_refresh_carry_error"])
        self.assertTrue(
            result.payload["card"]["engine_evidence_ref"].startswith(
                "engine-attestation:"
            )
        )

    def test_the_preview_card_is_the_card_a_reauthorization_publishes(self):
        preview = self._preview().payload["card"]
        before = self._fingerprint()

        run_cli(
            ["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )

        self.assertNotEqual(self._fingerprint(), before)
        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertIn("authorization_reauthorized", names)
        published = json.loads(
            (self.directory / "authorization-card.json").read_text(encoding="utf-8")
        )
        # Everything a reviewer can act on is identical; only the fields that
        # carry the re-observed attestation's timestamp may differ.  Both sides
        # go through the JSON shape so a tuple on one side and a list on the
        # other is not mistaken for a difference.
        self.assertEqual(
            {
                key: _normalize_card_value(value)
                for key, value in preview.items()
                if key not in _VOLATILE_CARD_FIELDS
            },
            {
                key: _normalize_card_value(value)
                for key, value in published.items()
                if key not in _VOLATILE_CARD_FIELDS
            },
        )


class FirstPublicationPreviewTests(unittest.TestCase):
    """Before a run exists, the preview digest *is* the digest that gets signed.

    That is the one case where the preview is a literal approval credential, so
    the flag that says so has to be right in both directions.
    """

    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload

    def test_the_first_publication_digest_is_the_one_that_gets_signed(self):
        result = run_cli(
            ["monitor", "--plan", "probe-plan", "--preview-card", "--json"], self.root
        )

        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(result.payload["publication"], "first")
        self.assertTrue(result.payload["preview_card_digest_matches_publication"])
        on_disk = json.loads(
            (
                self.paths.vibe / "plans" / "probe-plan" / "authorization-card.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(result.payload["preview_card_digest"], on_disk["digest"])


class ObservedAdapterAdmissionTests(unittest.TestCase):
    """The publish/dispatch gate must admit exactly what the ruling admits.

    ``_observed_adapter`` decides "is this platform dispatchable?" from the
    capability report.  It used to require a verified visible lifecycle
    outright, which refused the in-session SDD shape entirely; now it admits
    that shape -- but only for platforms whose matrix row actually rules it,
    so the gate can never be looser than the dispatcher.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vg-observed-adapter-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)
        self.assertEqual(
            run_cli(["init", "--confirm", "--json"], self.root).payload["status"], "ok"
        )

    def write(self, adapter_id, **facts):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_id": adapter_id,
                    "facts": facts,
                    "provenance": "test",
                }
            ),
            encoding="utf-8",
        )

    def test_admits_the_in_session_sdd_shape(self):
        self.write("workbuddy", **{"workbuddy.subprocess": True, "workbuddy.in_session_sdd": True})

        result = _observed_adapter(self.paths, "workbuddy")

        self.assertEqual(result.capabilities.mode, "in_session_sdd")
        self.assertTrue(result.capabilities.in_session_sdd)
        self.assertFalse(result.capabilities.visible_automation)

    def test_refuses_a_row_that_never_upgrades(self):
        # Byte-for-byte the same fact shape that workbuddy is admitted on;
        # only grok's matrix row differs (``probe_pass`` stays conservative).
        self.write("grok", **{"grok.subprocess": True, "grok.in_session_sdd": True})

        with self.assertRaises(ValueError) as caught:
            _observed_adapter(self.paths, "grok")

        self.assertIn("dispatch lifecycle", str(caught.exception))

    def test_refuses_a_pass_without_provenance(self):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_id": "workbuddy",
                    "facts": {
                        "workbuddy.subprocess": True,
                        "workbuddy.in_session_sdd": True,
                    },
                    "provenance": "",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            _observed_adapter(self.paths, "workbuddy")

        self.assertIn("dispatch lifecycle", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
