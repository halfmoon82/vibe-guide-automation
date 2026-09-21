"""A node id that merely *reads* like a secret name must stay loadable.

Node ids come from the product spec (`nodes[].id`) and only have to match
`models._ID`, so a feature about refreshing a token yields the perfectly legal
id ``token-refresh``.  The durable projection used to decide redaction from the
key alone, so every identifier-keyed map (`nodes`, `handles`, `tasks`,
`parallel_groups`, and the node-keyed event payloads) collapsed to the string
``"[REDACTED]"`` on the first save and `_validate_snapshot` then rejected the
snapshot forever: the run became permanently unloadable.

The two halves of the contract are asserted together here on purpose — the id
survives *and* a genuine ``token`` field inside the node is still redacted.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.cli import run_cli
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    RunSnapshot,
    append_event,
    load_events,
    load_snapshot,
    save_snapshot,
)

FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
REQUEST = "设计并实现保单查看页的 PDF 导出，集成日期范围筛选、编写测试并部署"
FACTS = {name: True for name in (
    "claude-code.agent", "claude-code.shell", "claude-code.subprocess", "claude-code.worktree",
    "claude-code.visible_task.create", "claude-code.visible_task.enter",
    "claude-code.visible_task.resume", "claude-code.visible_task.wait",
)}
SENSITIVE_IDS = (
    "token-refresh",
    "secret-rotation",
    "password-reset",
    "api_key-issuer",
    "private_key-store",
    "credential-vault",
)
# The same defect has a second half.  `_sanitize_durable_value` also rewrites a
# value whose key names provider text, and those names are bare words that make
# perfectly ordinary node ids.  On main a node called `reason` had its `status`
# replaced by "[REDACTED_PROVIDER_TEXT]" — not in _NODE_STATUSES, so the run was
# just as permanently unloadable, only via a different marker.
PROVIDER_TEXT_IDS = ("reason", "evidence", "output", "message", "error")
REDACTION_MARKERS = ("[REDACTED]", "[REDACTED_PROVIDER_TEXT]")


def identifier_redactions(payload, identifiers):
    """Every path where an identifier key maps to a redaction marker.

    Anchored on the identifier key itself rather than on a bare
    ``"[REDACTED]" not in text`` scan: the durable projection is *supposed* to
    redact genuine fields whose names contain a secret word (`token_required`
    in the workflow record is one), so a whole-file scan would fail for the
    right behaviour and hide the wrong one.  Both markers count — a node whose
    id names provider text is bricked by the second one.
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, item in node.items():
                child = path + [str(key)]
                if str(key) in identifiers and item in REDACTION_MARKERS:
                    found.append("/".join(child))
                walk(item, child)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, path + [str(index)])

    walk(payload, [])
    return found


class SensitiveNodeIdRoundTripTests(unittest.TestCase):
    """Snapshot level: save → load must survive a secret-looking node id."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = ProjectPaths(self.root)

    def snapshot(self, node_id, node_extra=None):
        plan = Plan("plan-1", 1, "docs/prd.md", [node_id], "draft")
        node = DAGNode(
            node_id,
            node_id,
            [],
            [],
            None,
            {"files": ["n1.py"], "worker": "worker-n1", "worktree": ".worktrees/n1"},
            "ready",
        )
        card = build_authorization_card(
            plan,
            [node],
            AgentCapabilities("fake", True, True, True, True, True, "full"),
        )
        record = authorize(card, "AUTHORIZE")
        append_event(
            self.paths,
            RunEvent(
                "run_started",
                {
                    "run_id": "run-1",
                    "authorization_digest": record.digest,
                    "node_contract_digest": record.node_contract_digest,
                    "node_ids": [node_id],
                },
            ),
        )
        node_state = {"status": "running"}
        node_state.update(node_extra or {})
        return RunSnapshot(
            "run-1",
            "plan-1",
            1,
            "running",
            {node_id: node_state},
            {node_id: "handle-1"},
            tasks={"{}:developer".format(node_id): {"node_id": node_id}},
            parallel_groups={node_id + "-group": [node_id]},
            authorization=record.to_dict(),
            authorization_digest=record.digest,
            node_contract_digest=record.node_contract_digest,
            event_sequence=1,
        )

    def test_every_sensitive_looking_node_id_survives_save_and_load(self):
        for node_id in SENSITIVE_IDS + PROVIDER_TEXT_IDS:
            with self.subTest(node_id=node_id):
                self.temporary.cleanup()
                self.temporary = tempfile.TemporaryDirectory()
                self.root = Path(self.temporary.name)
                self.paths = ProjectPaths(self.root)
                expected = self.snapshot(node_id)

                save_snapshot(self.paths, expected)

                self.assertEqual(load_snapshot(self.paths, "run-1"), expected)

    def test_provider_text_named_node_keeps_its_fields_verbatim(self):
        """`reason` is a legal node id and also a provider-text field name."""
        save_snapshot(self.paths, self.snapshot("reason"))

        persisted = json.loads(
            (self.root / ".vibe" / "runs" / "run-1" / "state.json").read_text(encoding="utf-8")
        )

        self.assertEqual(persisted["nodes"]["reason"], {"status": "running"})
        self.assertEqual(persisted["handles"]["reason"], "handle-1")
        self.assertEqual(
            identifier_redactions(persisted, {"reason", "reason:developer", "reason-group"}),
            [],
        )

    def test_provider_text_inside_a_node_is_still_redacted(self):
        """The provider-text rule keeps working one level in."""
        save_snapshot(
            self.paths,
            self.snapshot("reason", node_extra={"reason": "provider said something"}),
        )

        persisted = json.loads(
            (self.root / ".vibe" / "runs" / "run-1" / "state.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            persisted["nodes"]["reason"]["reason"], "[REDACTED_PROVIDER_TEXT]"
        )
        self.assertNotIn("provider said something", json.dumps(persisted, ensure_ascii=False))

    def test_identifier_keyed_maps_keep_their_shape_on_disk(self):
        node_id = "token-refresh"
        save_snapshot(self.paths, self.snapshot(node_id))

        persisted = json.loads(
            (self.root / ".vibe" / "runs" / "run-1" / "state.json").read_text(encoding="utf-8")
        )

        self.assertEqual(persisted["nodes"][node_id], {"status": "running"})
        self.assertEqual(persisted["handles"][node_id], "handle-1")
        self.assertEqual(persisted["tasks"][node_id + ":developer"], {"node_id": node_id})
        self.assertEqual(persisted["parallel_groups"][node_id + "-group"], [node_id])

    def test_genuine_secret_field_inside_a_node_is_still_redacted(self):
        node_id = "token-refresh"
        save_snapshot(
            self.paths,
            self.snapshot(node_id, node_extra={"token": "ghp-real-secret", "api_key": "sk-real"}),
        )

        persisted = json.loads(
            (self.root / ".vibe" / "runs" / "run-1" / "state.json").read_text(encoding="utf-8")
        )

        self.assertEqual(persisted["nodes"][node_id]["token"], "[REDACTED]")
        self.assertEqual(persisted["nodes"][node_id]["api_key"], "[REDACTED]")
        self.assertNotIn("ghp-real-secret", json.dumps(persisted, ensure_ascii=False))
        self.assertNotIn("sk-real", json.dumps(persisted, ensure_ascii=False))

    def test_exemption_stops_at_the_identifier_even_when_the_id_is_a_field_name(self):
        """The identifier exemption must be exactly one level deep.

        ``handles`` is a legal node id (models._ID allows it) and is also the
        name of an identifier-keyed map.  If the exemption were keyed on the
        name alone instead of on the position, the node's *own* fields would
        inherit it and a real secret inside that node would be written in the
        clear.
        """
        save_snapshot(
            self.paths,
            self.snapshot("handles", node_extra={"token": "ghp-real-secret"}),
        )

        persisted = json.loads(
            (self.root / ".vibe" / "runs" / "run-1" / "state.json").read_text(encoding="utf-8")
        )

        self.assertEqual(persisted["nodes"]["handles"]["status"], "running")
        self.assertEqual(persisted["nodes"]["handles"]["token"], "[REDACTED]")
        self.assertNotIn("ghp-real-secret", json.dumps(persisted, ensure_ascii=False))


def monitor_node(node_id):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "g1",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "worker_profile": {
                "worker": "codex", "model": "test", "reasoning": "normal",
                "fallbacks": [], "writer": "writer",
                "selection_basis": {
                    "issue_complexity_ref": node_id, "complexity_band": "standard",
                    "risk_tags": [], "availability_evidence": "test",
                },
                "worktree": ".worktrees/" + node_id,
                "branch": "branch-" + node_id,
                "allowlist": [node_id + ".py"],
            },
        },
        "ready",
    )


class SensitiveNodeIdReauthorizationTests(unittest.TestCase):
    """Recovery path: the reauthorization event carries identifier-keyed maps.

    `continuation`, `node_contract_digests` and the acceptance maps are all
    keyed by node id (or `<node>:<role>`), are persisted through
    `_sanitize_event_data`, and are read back field by field when an
    interrupted reauthorization replays.  Redacting them by key name bricks
    recovery with an error that names the evidence, never the node id.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(
            self.paths, build_contract(self.paths.root, provider="fake", host_id="local")
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def replay_interrupted_reauthorization(self, node_id):
        plan = Plan("plan-1", 1, "docs/prd.md", [node_id], "draft")
        original = monitor_node(node_id)
        record = authorize(
            build_authorization_card(plan, [original], self.capabilities), "AUTHORIZE"
        )
        runner = FakeRunner()
        snapshot = Monitor(self.paths, plan, [original]).start(record, runner)

        corrected = monitor_node(node_id)
        corrected.contract["acceptance_example"] = "corrected implementation outcome"
        corrected_record = authorize(
            build_authorization_card(plan, [corrected], self.capabilities), "AUTHORIZE"
        )
        corrected_monitor = Monitor(self.paths, plan, [corrected])
        # Crash after the event is appended but before the snapshot is saved,
        # so the next call must replay the persisted event.
        with patch.object(
            corrected_monitor,
            "_schedule_ready",
            side_effect=RuntimeError("interrupted after reauthorization event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                corrected_monitor.reauthorize(
                    snapshot.run_id, corrected_record, runner, "executable_contract_changed"
                )

        recovered = corrected_monitor.reauthorize(
            snapshot.run_id, corrected_record, FakeRunner(), "executable_contract_changed"
        )
        persisted = [
            record["data"]
            for record in load_events(self.paths, snapshot.run_id)
            if record["event"] == "authorization_reauthorized"
        ]
        return recovered, persisted

    def test_sensitive_looking_node_id_replays_an_interrupted_reauthorization(self):
        recovered, persisted = self.replay_interrupted_reauthorization("token-refresh")

        self.assertEqual(recovered.nodes["token-refresh"]["status"], "rework")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(
            persisted[0]["continuation"],
            {"token-refresh:developer": {"cursor": None, "task_id": "developer:token-refresh"}},
        )
        self.assertEqual(
            sorted(persisted[0]["node_contract_digests"]), ["token-refresh"]
        )
        self.assertEqual(
            sorted(persisted[0]["previous_node_contract_digests"]), ["token-refresh"]
        )
        self.assertEqual(sorted(persisted[0]["authorized_node_contracts"]), ["token-refresh"])
        for name in (
            "continuation",
            "node_contract_digests",
            "previous_node_contract_digests",
            "authorized_node_contracts",
        ):
            self.assertNotIn("[REDACTED]", json.dumps(persisted[0][name]), name)

    def test_plain_node_id_replays_the_same_way(self):
        """The control: without it, a broken replay reads as an unrelated bug."""
        recovered, persisted = self.replay_interrupted_reauthorization("plain-node")

        self.assertEqual(recovered.nodes["plain-node"]["status"], "rework")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(
            persisted[0]["continuation"],
            {"plain-node:developer": {"cursor": None, "task_id": "developer:plain-node"}},
        )

    def test_acceptance_maps_keep_their_sensitive_looking_node_keys(self):
        """`retained_acceptances` / `invalidated_acceptances` are node-keyed too.

        They only appear once a node has been accepted and a later
        reauthorization has to decide which acceptances survive the new
        contract, so reaching them takes a full accept-then-reauthorize run.
        """
        changed_id, kept_id = "token-refresh", "secret-rotation"
        nodes = [monitor_node(changed_id), monitor_node(kept_id)]
        plan = Plan("plan-1", 1, "docs/prd.md", [changed_id, kept_id], "draft")
        record = authorize(
            build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
        )
        runner = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": "delivery-" + node_id})]
                for node_id in (changed_id, kept_id)
            } | {
                (node_id, "reviewer"): [("accepted", {"evidence": "review-" + node_id})]
                for node_id in (changed_id, kept_id)
            }
        )
        monitor = Monitor(self.paths, plan, nodes)
        snapshot = monitor.start(record, runner)
        for _ in range(3):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes[changed_id]["status"], "accepted")
        self.assertEqual(snapshot.nodes[kept_id]["status"], "accepted")

        changed = monitor_node(changed_id)
        changed.contract["acceptance_example"] = "changed contract"
        changed_nodes = [changed, monitor_node(kept_id)]
        changed_record = authorize(
            build_authorization_card(plan, changed_nodes, self.capabilities), "AUTHORIZE"
        )

        reauthorized = Monitor(self.paths, plan, changed_nodes).reauthorize(
            snapshot.run_id, changed_record, FakeRunner(), "executable_contract_changed"
        )

        self.assertEqual(reauthorized.nodes[kept_id]["status"], "accepted")
        self.assertEqual(reauthorized.nodes[changed_id]["status"], "rework")
        data = [
            record["data"]
            for record in load_events(self.paths, snapshot.run_id)
            if record["event"] == "authorization_reauthorized"
        ][-1]
        self.assertEqual(sorted(data["retained_acceptances"]), [kept_id])
        self.assertEqual(sorted(data["invalidated_acceptances"]), [changed_id])
        for name in ("retained_acceptances", "invalidated_acceptances"):
            for node_id, evidence in data[name].items():
                self.assertIsInstance(evidence, dict, (name, node_id))


class SensitiveNodeIdJourneyTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v45-sensitive-id-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)

    def cli(self, *argv):
        return run_cli(list(argv) + ["--json"], self.root)

    def test_product_spec_node_named_token_refresh_reaches_a_loadable_run(self):
        spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
        spec["nodes"][0]["id"] = "token-refresh"
        spec["nodes"][1]["integration_after"] = ["token-refresh"]
        (self.root / "product-spec.json").write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8"
        )

        self.assertEqual(self.cli("init", "--confirm").payload["status"], "ok")
        (self.root / "facts.json").write_text(json.dumps(FACTS), encoding="utf-8")
        attested = self.cli(
            "attest", "--adapter", "claude-code", "--facts", "facts.json",
            "--provenance", "regression: sensitive-looking node id",
            "--project-id", "sensitiveidprobe",
        )
        self.assertEqual(attested.payload["status"], "ok", attested.payload)
        published = self.cli(
            "plan", "--request", REQUEST, "--plan-id", "pm-plan",
            "--from-prd", "product-spec.json",
        )
        self.assertEqual(published.payload["status"], "ok", published.payload)
        self.assertIn(
            "token-refresh",
            json.loads((self.root / ".vibe" / "plans" / "pm-plan" / "plan.json").read_text(encoding="utf-8"))["node_ids"],
        )
        authorized = self.cli("authorize", "--plan", "pm-plan", "--authorize", "AUTHORIZE")
        self.assertEqual(authorized.payload["status"], "ok", authorized.payload)

        monitored = self.cli("monitor", "--plan", "pm-plan", "--authorize", "AUTHORIZE")

        self.assertIn(
            monitored.payload["status"],
            {"retry_pending", "blocked_unknown", "running"},
            monitored.payload,
        )
        run_id = monitored.payload["run_id"]
        snapshot = load_snapshot(self.paths, run_id)
        self.assertEqual(
            set(snapshot.nodes),
            {"token-refresh", "date-range-filter", "integration-review"},
        )
        self.assertEqual(snapshot.nodes["token-refresh"].get("status"), "running")
        self.assertIn("token-refresh", snapshot.nodes["token-refresh"].get("branch", ""))
        run_directory = self.root / ".vibe" / "runs" / run_id
        identifiers = set(snapshot.nodes) | {
            "{}:{}".format(node_id, role)
            for node_id in snapshot.nodes
            for role in ("developer", "reviewer")
        }
        persisted = json.loads((run_directory / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(identifier_redactions(persisted, identifiers), [], "state.json")
        for line in (run_directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            self.assertEqual(
                identifier_redactions(record, identifiers), [], record["event"]
            )
        # Reading the run back through the public commands must work too.
        self.assertEqual(self.cli("status", "--plan", "pm-plan").payload.get("run_id"), run_id)
        self.assertEqual(self.cli("resume", "--plan", "pm-plan").payload.get("run_id"), run_id)


if __name__ == "__main__":
    unittest.main()
