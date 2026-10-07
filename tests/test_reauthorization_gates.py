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
import unittest
from unittest.mock import patch

from vibe_guide.authorize_entry import load_live_workflow
from vibe_guide.authorization import (
    BACKGROUND_MODE_DISCLOSURES,
    authorize,
    build_authorization_card,
    dispatch_topology_for_node,
    refresh_authorization_card,
)
from vibe_guide.cli import _load_plan, run_cli
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
        node = _business_node("integration-review")

        self.assertEqual(
            dispatch_topology_for_node(node, {"workbuddy": "in_session_sdd"}),
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


if __name__ == "__main__":
    unittest.main()
