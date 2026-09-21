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

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.cli import run_cli
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import (
    RunSnapshot,
    append_event,
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


def identifier_redactions(payload, identifiers):
    """Every path where an identifier key maps to the redaction marker.

    Anchored on the identifier key itself rather than on a bare
    ``"[REDACTED]" not in text`` scan: the durable projection is *supposed* to
    redact genuine fields whose names contain a secret word (`token_required`
    in the workflow record is one), so a whole-file scan would fail for the
    right behaviour and hide the wrong one.
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, item in node.items():
                child = path + [str(key)]
                if str(key) in identifiers and item == "[REDACTED]":
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
        for node_id in SENSITIVE_IDS:
            with self.subTest(node_id=node_id):
                self.temporary.cleanup()
                self.temporary = tempfile.TemporaryDirectory()
                self.root = Path(self.temporary.name)
                self.paths = ProjectPaths(self.root)
                expected = self.snapshot(node_id)

                save_snapshot(self.paths, expected)

                self.assertEqual(load_snapshot(self.paths, "run-1"), expected)

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


class SensitiveNodeIdJourneyTests(unittest.TestCase):
    """CLI level: the id really does arrive from the product spec."""

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
