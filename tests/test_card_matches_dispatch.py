"""The authorization card describes the dispatch that actually happens.

PRD 5.2.6 card-and-handoff: a new plan stamps every node's dispatch topology
into its contract at publication, dispatch reads only that stamp, and the
stamp is a routing leaf (a handoff may move it without touching the
acceptance contract).  The integration-review closeout on Claude Code is
background on the card exactly as it is in the task registry.  Plans
authorized before the stamp existed keep ruling live and stay authorized.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import authorization
from vibe_guide.authorization import (
    DISPATCH_TOPOLOGY_KEY,
    acceptance_contract_projection,
    dispatch_topology_for_node,
    rebind_dispatch_contract,
    ruled_dispatch_topology,
    stamp_dispatch_topology,
)
from vibe_guide.cli import UNSTAMPED_PLAN_WARNING, _load_plan, run_cli
from vibe_guide.models import DAGNode
from vibe_guide.paths import ProjectPaths
from vibe_guide.task_registry import TaskBinding

import tests.support_v45_authorize as support
from tests.test_claude_bg_wiring import BG, LAUNCHER_MODULE, inject, launcher_module
from tests.test_platform_handoff_rebind import FIRST, PLAN, SECOND, HandoffCase


def node(node_id="n1", adapter="claude-code", **contract):
    return DAGNode.from_dict({
        "id": node_id, "title": node_id, "depends_on": [], "integration_after": [],
        "parallel_group": None, "status": "planned",
        "contract": dict({"adapter_id": adapter, "files": ["a.py"],
                          "input": "i", "output": "o", "error_behavior": "e",
                          "acceptance_example": "x"}, **contract),
    })


def _plan_dir(root, plan_id="probe-plan"):
    return Path(root) / ".vibe" / "plans" / plan_id


class PublicationStampsTheDispatchTests(unittest.TestCase):
    """Acceptance 1: Claude Code publication with an integration review."""

    def publish(self, dispatch_mode=None, background_launch=True):
        """Publish on Claude Code; ``background_launch`` attests the verified launcher."""
        if not background_launch or dispatch_mode == "visible":
            return support.publish_complex_probe(self, dispatch_mode=dispatch_mode)
        capabilities = dict(support._CAPABILITIES, facts=dict(
            support._CAPABILITIES["facts"], **{BG: True}))
        with mock.patch.object(support, "_CAPABILITIES", capabilities), \
                inject(**{LAUNCHER_MODULE: launcher_module()}):
            return support.publish_complex_probe(self, dispatch_mode=dispatch_mode)

    def test_claude_code_integration_review_is_background_on_card_and_in_contract(self):
        root = self.publish()
        card = json.loads((_plan_dir(root) / "authorization-card.json").read_text(encoding="utf-8"))
        workers = {item["node_id"]: item for item in card["workers"]}
        review = workers["integration-review"]
        self.assertEqual(review["mode"], "background")
        self.assertEqual(review["topology"], "background")
        self.assertEqual(review["role"], "reviewer")
        self.assertEqual(authorization._background_limitation_gaps(tuple(review["limitations"])), [])
        # Every node contract carries the stamp the card was built from.
        nodes = {item["id"]: item for item in json.loads(
            (_plan_dir(root) / "nodes.json").read_text(encoding="utf-8"))}
        self.assertEqual(nodes["integration-review"]["contract"][DISPATCH_TOPOLOGY_KEY], "background")
        self.assertEqual(nodes["probe-node-a"]["contract"][DISPATCH_TOPOLOGY_KEY], "background")
        for node_id, entry in workers.items():
            self.assertEqual(entry["topology"], nodes[node_id]["contract"][DISPATCH_TOPOLOGY_KEY])
        self.assertEqual(card["topology_summary"]["background"], 2)
        self.assertEqual(card["topology_summary"]["visible"], 0)

    def test_task_registry_mode_for_the_stamped_closeout_is_background(self):
        root = self.publish()
        paths = ProjectPaths(root)
        _directory, _plan, nodes, card = _load_plan(paths, "probe-plan")
        review = next(item for item in nodes if item.id == "integration-review")
        # What the monitor dispatches the closeout under ...
        topology = dispatch_topology_for_node(review, {"claude-code": "dual-visible"})
        self.assertEqual(topology, "background")
        # ... can only be registered as a background binding, which is what
        # the card says.
        entry = next(item for item in card.workers if item["node_id"] == "integration-review")
        binding = TaskBinding(provider="claude-code-background", mode=entry["mode"],
                              issue_id="integration-review", role="reviewer", topology=topology,
                              task_id="t", run_id="run-x", generation=1, status="running",
                              worktree=".worktrees/x", branch="b", visible=False,
                              limitations=list(entry["limitations"]))
        self.assertEqual(binding.mode, "background")
        with self.assertRaisesRegex(ValueError, "background topology requires background mode"):
            TaskBinding(provider="claude-code-background", mode="visible",
                        issue_id="integration-review", role="reviewer", topology=topology,
                        task_id="t", run_id="run-x", generation=1, status="running",
                        worktree=".worktrees/x", branch="b", visible=True,
                        limitations=list(entry["limitations"]))

    def test_default_background_without_a_launcher_stamps_what_can_be_dispatched(self):
        """No verified background launch: the runner is the visible one, so is the card."""
        root = self.publish(background_launch=False)
        card = json.loads((_plan_dir(root) / "authorization-card.json").read_text(encoding="utf-8"))
        nodes = {item["id"]: item for item in json.loads(
            (_plan_dir(root) / "nodes.json").read_text(encoding="utf-8"))}
        self.assertNotEqual(nodes["probe-node-a"]["contract"][DISPATCH_TOPOLOGY_KEY], "background")
        self.assertEqual((card.get("topology_summary") or {}).get("background", 0), 0)

    def test_visible_choice_stamps_visible_and_the_card_agrees(self):
        root = self.publish(dispatch_mode="visible")
        card = json.loads((_plan_dir(root) / "authorization-card.json").read_text(encoding="utf-8"))
        nodes = {item["id"]: item for item in json.loads(
            (_plan_dir(root) / "nodes.json").read_text(encoding="utf-8"))}
        self.assertEqual(nodes["integration-review"]["contract"][DISPATCH_TOPOLOGY_KEY], "dual-visible")
        for entry in card.get("workers") or ():
            self.assertNotEqual(entry["mode"], "background")
            self.assertEqual(entry["topology"], nodes[entry["node_id"]]["contract"][DISPATCH_TOPOLOGY_KEY])


class StampIsTheDispatchTests(unittest.TestCase):
    def test_dispatch_reads_the_stamp_not_the_live_ruling(self):
        stamped = node(**{DISPATCH_TOPOLOGY_KEY: "visible-sdd"})
        self.assertEqual(dispatch_topology_for_node(stamped, {"claude-code": "background"}), "visible-sdd")

    def test_unstamped_contract_keeps_the_live_ruling(self):
        self.assertEqual(dispatch_topology_for_node(node(), {"claude-code": "background"}), "background")
        self.assertEqual(dispatch_topology_for_node(node(), {"claude-code": "in_session_sdd"}), "visible-sdd")
        # An unstamped closeout keeps the pre-5.2.6 ruling: old runs do not move.
        review = node("integration-review")
        self.assertEqual(dispatch_topology_for_node(review, {"claude-code": "background"}), "dual-visible")

    def test_closeout_is_never_stamped_visible_sdd(self):
        review = node("integration-review")
        self.assertEqual(ruled_dispatch_topology(review, {"claude-code": "in_session_sdd"}), "dual-visible")
        self.assertEqual(ruled_dispatch_topology(review, {"claude-code": "background"}), "background")

    def test_stamping_keeps_an_existing_value(self):
        nodes = [node("a", **{DISPATCH_TOPOLOGY_KEY: "dual-visible"}), node("b")]
        self.assertEqual(stamp_dispatch_topology(nodes, {"claude-code": "background"}),
                         {"a": "dual-visible", "b": "background"})

    def test_the_stamp_is_a_routing_leaf(self):
        before = node(**{DISPATCH_TOPOLOGY_KEY: "visible-sdd"})
        after = DAGNode.from_dict(dict(before.to_dict(), contract=rebind_dispatch_contract(
            before.contract, "claude-code", "background")))
        self.assertEqual(after.contract[DISPATCH_TOPOLOGY_KEY], "background")
        self.assertEqual(acceptance_contract_projection(before), acceptance_contract_projection(after))
        # Anything else that changes is still an acceptance change.
        edited = DAGNode.from_dict(dict(after.to_dict(), contract=dict(after.contract, output="other")))
        self.assertNotEqual(acceptance_contract_projection(before), acceptance_contract_projection(edited))

    def test_rebind_never_stamps_an_old_contract(self):
        old = node()
        rebound = rebind_dispatch_contract(old.contract, "codex", "visible-sdd")
        self.assertNotIn(DISPATCH_TOPOLOGY_KEY, rebound)

    def test_card_follows_a_moved_stamp_in_both_directions(self):
        moved = node(**{DISPATCH_TOPOLOGY_KEY: "visible-sdd"})
        background_entry = {"node_id": "n1", "topology": "background", "mode": "background",
                            "role": "developer", "session_source": "", "limitations": []}
        (entry,) = authorization._rerule_workers([background_entry], [moved], {})
        self.assertEqual((entry["topology"], entry["mode"]), ("visible-sdd", "visible"))
        to_background = node(**{DISPATCH_TOPOLOGY_KEY: "background"})
        visible_entry = dict(background_entry, topology="visible-sdd", mode="visible")
        (entry,) = authorization._rerule_workers([visible_entry], [to_background], {},
                                                 {"n1": "平台交接 codex → claude-code：x"})
        self.assertEqual((entry["topology"], entry["mode"]), ("background", "background"))
        self.assertEqual(authorization._background_limitation_gaps(tuple(entry["limitations"])), [])
        self.assertIn("平台交接 codex → claude-code：x", entry["limitations"])


class OldRunsStayAuthorizedTests(HandoffCase):
    """Acceptance 5: a run authorized before the stamp existed (5.2.5 shape)."""

    def publish_unstamped(self):
        with mock.patch("vibe_guide.cli.stamp_dispatch_topology",
                        lambda nodes, rulings: {n.id: dispatch_topology_for_node(n, rulings) for n in nodes}):
            snapshot = self.published_on("workbuddy")
        for item in json.loads((self.root / ".vibe" / "plans" / PLAN / "nodes.json").read_text(encoding="utf-8")):
            self.assertNotIn(DISPATCH_TOPOLOGY_KEY, item["contract"])
        return snapshot

    def test_resume_after_upgrade_does_not_invalidate_the_authorization(self):
        before = self.publish_unstamped()
        self.assertEqual(before.nodes[FIRST]["status"], "accepted")
        resumed = self.cli("resume", "--plan", PLAN)
        self.assertEqual(resumed.payload.get("run_id"), self.run_id, resumed.payload)
        self.assertNotIn(resumed.payload.get("status"), {"blocked_design", "blocked_invalid"}, resumed.payload)
        self.assertFalse((self.root / ".vibe" / "plans" / PLAN / "authorization-invalidated.json").exists())
        self.assertNotEqual(self.snapshot().nodes[SECOND]["status"], "blocked_unknown")
        self.serve(SECOND)
        self.assertEqual(self.snapshot().nodes[SECOND]["status"], "accepted")

    def test_doctor_names_the_unstamped_plan_once(self):
        self.publish_unstamped()
        result = self.cli("doctor")
        hits = [item for item in result.payload["warnings"] if "没有写入派发方式" in item]
        self.assertEqual(hits, [UNSTAMPED_PLAN_WARNING.format(PLAN)])

    def test_doctor_is_silent_for_a_stamped_plan(self):
        self.published_on("workbuddy")
        result = self.cli("doctor")
        self.assertFalse([item for item in result.payload["warnings"] if "没有写入派发方式" in item])


if __name__ == "__main__":
    unittest.main()
