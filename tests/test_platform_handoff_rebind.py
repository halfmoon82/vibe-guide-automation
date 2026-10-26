"""`vibe monitor --platform-handoff` must move unfinished nodes to the new platform.

5.1.6-5.1.8 only kept the authorization alive and the accepted nodes accepted.
Nothing re-pointed the unfinished nodes: nodes.json and the card kept naming
the old platform, so the first dispatch on the new one was refused as a
provider-action identity drift and the node parked at ``blocked_unknown``.

Every test drives the public CLI against the real provider mailbox (nothing
hand-edits ``.vibe/``), the same way a supervisor would after a platform switch.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
from unittest.mock import patch

from vibe_guide.authorization import acceptance_contract_projection
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.cli import _load_plan, run_cli
from vibe_guide.handoff_rebind import (
    HandoffRebindError,
    rebind_nodes,
    resolve_handoff_target,
    unfinished_node_ids,
)
from vibe_guide.models import DAGNode
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events, load_snapshot

from tests import test_integration_review_closeout_entry as closeout

CLEARED_CLAIM = closeout.CLEARED_CLAIM
FIXTURE = closeout.FIXTURE
REQUEST = closeout.REQUEST

PLAN = "hp"
FIRST, SECOND = "export-button", "date-range-filter"
PROBES = (
    "agent", "shell", "subprocess", "worktree", "visible_task.create",
    "visible_task.enter", "visible_task.resume", "visible_task.wait", "in_session_sdd",
)


def facts(adapter):
    return {adapter + "." + name: True for name in PROBES}


class HandoffCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="h5-rebind-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)
        self.store = ProviderActionStore(self.paths)
        self.waits = {}

    def cli(self, *argv):
        return run_cli(list(argv) + ["--json"], self.root)

    def attest(self, adapter, name=None):
        name = name or adapter + "-facts.json"
        (self.root / name).write_text(json.dumps(facts(adapter)), encoding="utf-8")
        result = self.cli("attest", "--adapter", adapter, "--facts", name,
                          "--provenance", "rebind test", "--project-id", "h5probe")
        self.assertEqual(result.payload["status"], "ok", result.payload)

    def switch_platform(self, adapter, register=True):
        save_contract(self.paths, build_contract(self.root, provider=adapter, host_id="local"))
        if register:
            self.attest(adapter)

    def serve(self, node_id, rounds=40):
        """Answer the mailbox for one node until it is accepted."""
        for _ in range(rounds):
            for action in self.store.pending():
                if action["issue_id"] == node_id:
                    self.store.complete(
                        action["action_id"],
                        closeout.MailboxClosesTheRunTests.reply(action, self.waits, CLEARED_CLAIM),
                    )
            self.cli("resume", "--plan", PLAN)
            if self.snapshot().nodes[node_id]["status"] == "accepted":
                return
        self.fail("node never reached accepted: " + node_id)

    def snapshot(self):
        return load_snapshot(self.paths, self.run_id)

    def published_on(self, adapter):
        """First node accepted, second ready with an unclaimed create on ``adapter``."""
        self.assertEqual(self.cli("init", "--confirm").payload["status"], "ok")
        self.attest(adapter)
        spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
        spec["nodes"][1]["depends_on"] = [FIRST]
        spec["nodes"][1]["integration_after"] = []
        (self.root / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        planned = self.cli("plan", "--request", REQUEST, "--plan-id", PLAN, "--s1", "5,5,5,5,5",
                           "--from-prd", "spec.json", "--adapter", adapter)
        self.assertEqual(planned.payload["status"], "ok", planned.payload)
        self.assertEqual(self.cli("authorize", "--plan", PLAN, "--authorize", "AUTHORIZE").payload["status"], "ok")
        started = self.cli("monitor", "--plan", PLAN, "--authorize", "AUTHORIZE")
        self.run_id = started.payload["run_id"]
        self.serve(FIRST)
        return self.snapshot()

    def handoff(self, *extra):
        return self.cli("monitor", "--plan", PLAN, "--authorize", "AUTHORIZE",
                        "--platform-handoff", *extra)

    def plan_nodes(self):
        return {node.id: node for node in _load_plan(self.paths, PLAN)[2]}

    def plan_card(self):
        return _load_plan(self.paths, PLAN)[3]


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class PlatformHandoffRebindTests(HandoffCase):
    def ready_unclaimed(self):
        snapshot = self.published_on("workbuddy")
        self.assertEqual(snapshot.nodes[FIRST]["status"], "accepted")
        self.assertEqual(snapshot.nodes[SECOND]["status"], "running")
        pending = self.store.pending()
        self.assertEqual([(a["issue_id"], a["provider"]) for a in pending],
                         [(SECOND, "workbuddy-visible")])
        return snapshot

    def node_file(self):
        return {item["id"]: item for item in _read(
            self.root / ".vibe" / "plans" / PLAN / "nodes.json")}

    def test_the_unfinished_node_is_claimed_and_accepted_on_the_new_platform(self):
        before = self.ready_unclaimed()
        old_create = self.store.pending()[0]["action_id"]
        self.switch_platform("claude-code")
        result = self.handoff()
        self.assertNotEqual(result.payload["status"], "blocked_unknown", result.payload)
        after = self.snapshot()
        self.assertNotEqual(after.nodes[SECOND]["status"], "blocked_unknown", after.nodes[SECOND])
        # The second node's new create goes out under the new platform's provider.
        pending = self.store.pending()
        self.assertEqual([(a["issue_id"], a["provider"]) for a in pending],
                         [(SECOND, "claude-code-visible")])
        self.assertNotEqual(pending[0]["action_id"], old_create)
        self.serve(SECOND)
        done = self.snapshot()
        self.assertEqual(done.nodes[SECOND]["status"], "accepted")
        # The node accepted before the switch keeps its evidence untouched.
        self.assertEqual(done.nodes[FIRST]["status"], "accepted")
        self.assertEqual(done.nodes[FIRST]["developer_identity"], before.nodes[FIRST]["developer_identity"])
        self.assertEqual(done.nodes[FIRST]["acceptance"]["contract_digest"],
                         before.nodes[FIRST]["acceptance"]["contract_digest"])

    def test_the_new_platform_create_does_not_collide_with_the_hidden_old_one(self):
        """Why no action-id surgery is needed.

        The id is derived from (operation, provider, run, issue, role,
        generation, sequence).  Rebinding changes the provider, so the new
        create has its own id and the old request stays an immutable, stale-
        marked file.  The old collision came from the provider not moving.
        """
        self.ready_unclaimed()
        old = self.store.pending()[0]
        self.switch_platform("claude-code")
        self.handoff()
        new = self.store.pending()[0]
        self.assertNotEqual((old["provider"], old["action_id"]), (new["provider"], new["action_id"]))
        stale = self.root / ".vibe" / "provider-actions" / "stale" / (old["action_id"] + ".json")
        self.assertTrue(stale.is_file())
        self.assertTrue((self.root / ".vibe" / "provider-actions" / "requests" / (old["action_id"] + ".json")).is_file())

    def test_the_rebind_is_persisted_so_resume_reads_the_new_platform(self):
        self.ready_unclaimed()
        self.switch_platform("claude-code")
        self.handoff()
        nodes = self.plan_nodes()
        self.assertEqual(nodes[SECOND].contract["adapter_id"], "claude-code")
        self.assertEqual(nodes["integration-review"].contract["adapter_id"], "claude-code")
        self.assertEqual(self.plan_card().agent_id, "claude-code")
        authorization = _read(self.root / ".vibe" / "plans" / PLAN / "authorization.json")
        self.assertEqual(authorization["agent_id"], "claude-code")
        # A plain resume (no flag) accepts the persisted state and dispatches.
        resumed = self.cli("resume", "--plan", PLAN)
        self.assertEqual(resumed.payload["run_id"], self.run_id, resumed.payload)
        self.assertNotEqual(resumed.payload["nodes"][SECOND]["status"], "blocked_unknown", resumed.payload)
        self.assertEqual([(a["issue_id"], a["provider"]) for a in self.store.pending()],
                         [(SECOND, "claude-code-visible")])
        self.serve(SECOND)
        self.assertEqual(self.snapshot().nodes[SECOND]["status"], "accepted")

    def test_the_accepted_node_binding_is_not_rewritten(self):
        self.ready_unclaimed()
        before = self.node_file()[FIRST]
        self.switch_platform("claude-code")
        self.handoff()
        after = self.node_file()
        self.assertEqual(after[FIRST], before)
        self.assertEqual(after[FIRST]["contract"]["adapter_id"], "workbuddy")
        self.assertEqual(after[SECOND]["contract"]["adapter_id"], "claude-code")

    def test_only_routing_leaves_change_so_the_acceptance_projection_is_identical(self):
        self.ready_unclaimed()
        before = self.plan_nodes()
        self.switch_platform("claude-code")
        self.handoff()
        after = self.plan_nodes()
        for node_id, node in before.items():
            self.assertEqual(acceptance_contract_projection(after[node_id]),
                             acceptance_contract_projection(node), node_id)
            self.assertEqual(after[node_id].contract["files"], node.contract["files"], node_id)
            self.assertEqual(after[node_id].depends_on, node.depends_on, node_id)

    def test_an_unattested_new_platform_is_a_readable_blocked_unknown_and_changes_nothing(self):
        self.ready_unclaimed()
        nodes_before = self.node_file()
        card_before = _read(self.root / ".vibe" / "plans" / PLAN / "authorization-card.json")
        self.switch_platform("claude-code", register=False)
        result = self.handoff("--adapter", "claude-code")
        self.assertEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertIn("vibe attest --adapter claude-code", result.payload["reason"])
        self.assertEqual(self.node_file(), nodes_before)
        self.assertEqual(_read(self.root / ".vibe" / "plans" / PLAN / "authorization-card.json"), card_before)
        self.assertEqual(len([e for e in load_events(self.paths, self.run_id)
                              if e["event"] == "authorization_reauthorized"]), 0)

    def test_a_platform_without_a_dispatch_lifecycle_is_refused_the_same_way(self):
        self.ready_unclaimed()
        nodes_before = self.node_file()
        self.switch_platform("claude-code", register=False)
        # grok attests but its matrix row never upgrades to a dispatchable shape.
        (self.root / "grok.json").write_text(json.dumps(facts("grok")), encoding="utf-8")
        self.cli("attest", "--adapter", "grok", "--facts", "grok.json", "--provenance", "x")
        result = self.handoff("--adapter", "grok")
        self.assertEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertIn("grok", result.payload["reason"])
        self.assertEqual(self.node_file(), nodes_before)

    def test_without_a_named_platform_two_dispatchable_candidates_are_ambiguous(self):
        self.ready_unclaimed()
        nodes_before = self.node_file()
        # The contract's own provider is a third platform, so it cannot break
        # the tie between the two attested candidates.
        save_contract(self.paths, build_contract(self.root, provider="cursor", host_id="local"))
        self.attest("claude-code")
        self.attest("codex")
        result = self.handoff()
        self.assertEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertIn("--adapter", result.payload["reason"])
        self.assertEqual(self.node_file(), nodes_before)

    def test_an_explicit_adapter_resolves_the_ambiguity(self):
        self.ready_unclaimed()
        self.switch_platform("claude-code")
        self.attest("codex")
        result = self.handoff("--adapter", "claude-code")
        self.assertNotEqual(result.payload["status"], "blocked_unknown", result.payload)
        self.assertEqual(self.plan_card().agent_id, "claude-code")

    def test_a_run_without_a_handoff_record_exempts_no_node_from_the_routing_check(self):
        from vibe_guide.cli import _handed_off_accepted_nodes, _public_runner

        self.ready_unclaimed()
        _directory, _plan, nodes, card = _load_plan(self.paths, PLAN)
        self.assertEqual(_handed_off_accepted_nodes(self.paths, card), frozenset())
        # Simulate a stray accepted node naming another platform: with no
        # recorded handoff the strict check still refuses it.
        stray = [n for n in nodes if n.id == FIRST][0]
        stray.contract["adapter_id"] = "cursor"
        with self.assertRaisesRegex(ValueError, "routing does not match"):
            _public_runner(self.paths, card, nodes)

    def test_an_ordinary_reauthorization_never_rebinds(self):
        self.ready_unclaimed()
        nodes_before = self.node_file()
        self.switch_platform("claude-code")
        result = self.cli("monitor", "--plan", PLAN, "--authorize", "AUTHORIZE")
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertEqual(self.node_file(), nodes_before)
        self.assertEqual(self.plan_card().agent_id, "workbuddy")

    def test_rerunning_a_finished_handoff_does_not_flip_the_run_back(self):
        self.ready_unclaimed()
        self.switch_platform("claude-code")
        self.handoff()
        # The old platform is still attested and dispatchable; it must not win.
        again = self.handoff()
        self.assertNotEqual(again.payload["status"], "blocked_unknown", again.payload)
        self.assertEqual(self.plan_card().agent_id, "claude-code")
        self.assertEqual(self.plan_nodes()[SECOND].contract["adapter_id"], "claude-code")

    def test_a_crash_between_the_run_move_and_the_file_write_converges_on_rerun(self):
        from vibe_guide import cli as cli_module

        self.ready_unclaimed()
        self.switch_platform("claude-code")
        real = cli_module._atomic_json

        def crash(path, payload):
            if Path(path).name == "nodes.json":
                raise OSError("injected crash before nodes.json")
            return real(path, payload)

        with patch.object(cli_module, "_atomic_json", crash):
            interrupted = self.handoff()
        self.assertNotEqual(interrupted.payload.get("status"), "running", interrupted.payload)
        # The run moved, the files did not: exactly the half-written state.
        self.assertEqual(self.snapshot().authorization["agent_id"], "claude-code")
        self.assertEqual(self.plan_nodes()[SECOND].contract["adapter_id"], "workbuddy")
        rerun = self.handoff()
        self.assertNotEqual(rerun.payload["status"], "blocked_unknown", rerun.payload)
        self.assertEqual(self.plan_nodes()[SECOND].contract["adapter_id"], "claude-code")
        self.assertEqual(self.plan_card().agent_id, "claude-code")
        self.serve(SECOND)
        self.assertEqual(self.snapshot().nodes[SECOND]["status"], "accepted")

    def test_exactly_one_writer_lease_exists_for_the_moved_node(self):
        self.ready_unclaimed()
        self.switch_platform("claude-code")
        self.handoff()
        leases = [json.loads(p.read_text()) for p in sorted((self.root / ".vibe" / "leases").glob("*.json"))
                  if json.loads(p.read_text())["node_id"] == SECOND]
        self.assertEqual([lease["status"] for lease in leases], ["active"])


class ResolveHandoffTargetTests(unittest.TestCase):
    def resolve(self, **overrides):
        args = dict(explicit=None, card_agent="a", run_agent="a", run_drift=False,
                    capability_changed=False, contract_provider=None, dispatchable_others=())
        args.update(overrides)
        return resolve_handoff_target(**args)

    def test_the_operator_named_platform_wins(self):
        self.assertEqual(self.resolve(explicit="b", dispatchable_others=("c",), capability_changed=True), "b")

    def test_an_interrupted_handoff_is_finished_from_the_run_not_from_observation(self):
        self.assertEqual(self.resolve(run_agent="b", dispatchable_others=("c", "a"), capability_changed=True), "b")
        self.assertEqual(self.resolve(run_agent="a", run_drift=True), "a")

    def test_nothing_moves_when_the_capability_contract_did_not_change(self):
        self.assertIsNone(self.resolve(dispatchable_others=("b",)))

    def test_one_other_dispatchable_platform_is_the_target(self):
        self.assertEqual(self.resolve(capability_changed=True, dispatchable_others=("a", "b")), "b")

    def test_the_contracts_own_provider_breaks_a_tie(self):
        self.assertEqual(self.resolve(capability_changed=True, contract_provider="c",
                                      dispatchable_others=("a", "b", "c")), "c")

    def test_several_candidates_without_a_tiebreak_is_an_error(self):
        with self.assertRaises(HandoffRebindError):
            self.resolve(capability_changed=True, dispatchable_others=("b", "c"))

    def test_no_other_platform_means_nothing_moves(self):
        self.assertIsNone(self.resolve(capability_changed=True, dispatchable_others=("a",)))


class RebindNodesTests(unittest.TestCase):
    @staticmethod
    def node(node_id, adapter="old", **contract):
        base = {"files": [node_id + ".py"], "adapter_id": adapter, "worker": "worker",
                "reviewer_worker": "reviewer", "input": "i", "output": "o",
                "error_behavior": "e", "acceptance_example": "x",
                "worker_profile": {"worker": "worker", "model": "m", "reasoning": "r",
                                   "fallbacks": [], "selection_basis": {"k": "v"}}}
        base.update(contract)
        return DAGNode(node_id, node_id, [], [], None, base, "planned")

    def test_only_the_movable_nodes_change_and_accepted_ones_are_the_same_objects(self):
        done, todo = self.node("done"), self.node("todo")
        result, moved = rebind_nodes([done, todo], ["todo"], "new")
        self.assertIs(result[0], done)
        self.assertEqual(result[1].contract["adapter_id"], "new")
        self.assertEqual(moved, ["todo"])
        self.assertEqual(done.contract["adapter_id"], "old")

    def test_a_node_already_on_the_target_is_not_reported_as_moved(self):
        result, moved = rebind_nodes([self.node("n", adapter="new")], ["n"], "new")
        self.assertEqual(moved, [])

    def test_routing_only_acceptance_projection_is_preserved(self):
        for contract in ({}, {"worker": "old", "reviewer_worker": "old"},
                         {"worker_profile": {"worker": "old", "model": "m", "reasoning": "r",
                                             "fallbacks": [], "selection_basis": {"k": "v"}}}):
            node = self.node("n", **contract)
            result, _ = rebind_nodes([node], ["n"], "new")
            self.assertEqual(acceptance_contract_projection(result[0]),
                             acceptance_contract_projection(node), contract)

    def test_a_worker_label_is_only_renamed_when_it_was_the_old_platform_id(self):
        result, _ = rebind_nodes([self.node("n", worker="old", reviewer_worker="reviewer")], ["n"], "new")
        self.assertEqual(result[0].contract["worker"], "new")
        self.assertEqual(result[0].contract["reviewer_worker"], "reviewer")
        self.assertEqual(result[0].contract["worker_profile"]["worker"], "worker")

    def test_model_and_reasoning_are_never_invented(self):
        result, _ = rebind_nodes([self.node("n")], ["n"], "new")
        profile = result[0].contract["worker_profile"]
        self.assertEqual((profile["model"], profile["reasoning"]), ("m", "r"))

    def test_unfinished_node_ids_excludes_accepted_and_refuses_an_unknown_node(self):
        nodes = [self.node("a"), self.node("b"), self.node("c")]
        snapshot = {"a": {"status": "accepted"}, "b": {"status": "running"}, "c": {"status": "planned"}}
        self.assertEqual(unfinished_node_ids(nodes, snapshot), ["b", "c"])
        with self.assertRaises(HandoffRebindError):
            unfinished_node_ids(nodes, {"a": {"status": "accepted"}})


if __name__ == "__main__":
    unittest.main()
