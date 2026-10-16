import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.decision_boundary import (
    DecisionMailbox,
    NotificationBridge,
    ingest_worker_decisions,
)
from vibe_guide.cli import UNKNOWN, run_cli
from vibe_guide.paths import ProjectPaths


class DecisionMailboxTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, {"VIBE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.paths = ProjectPaths.from_cwd(self.root)
        self.mailbox = DecisionMailbox(self.paths)

    def write_bridge(self, project, user=None):
        config_path = self.root / ".vibe" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"notify_bridge": project}), encoding="utf-8")
        if user is not None:
            (self.home / "config.json").write_text(
                json.dumps({"notify_bridge": user}), encoding="utf-8"
            )

    def test_raise_from_supervisor_and_worker_share_one_durable_request(self):
        first = self.mailbox.raise_decision(
            run_id="run-1",
            node_id="issue-1",
            question="Use A or B?",
            options=[{"id": "A", "label": "A", "impact": "fast"}, {"id": "B", "label": "B", "impact": "safe"}],
            recommendation="A",
            evidence={"source": "supervisor"},
        )
        second = self.mailbox.ingest_worker_requests(
            "run-1",
            "issue-1",
            [{
                "question": "Use A or B?",
                "options": first["options"],
                "recommendation": "A",
                "evidence": {"source": "worker"},
            }],
        )[0]
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(len(self.mailbox.list_pending("run-1")), 1)
        self.assertEqual(len(list((self.root / ".vibe" / "decisions" / "requests").glob("*.json"))), 1)

    def test_answer_is_rejected_when_option_digest_changes(self):
        request = self.mailbox.raise_decision(
            run_id="run-1", node_id="issue-1", question="Pick", options=["A", "B"]
        )
        path = self.root / ".vibe" / "decisions" / "requests" / (request["decision_id"] + ".json")
        changed = json.loads(path.read_text())
        changed["options"] = [{"id": "A", "label": "Changed", "impact": "new"}, {"id": "B", "label": "B", "impact": ""}]
        path.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            self.mailbox.answer(request["decision_id"], "A", request_digest=request["request_digest"])

    def test_notify_bridge_is_disabled_by_default_and_failure_is_unknown(self):
        bridge = NotificationBridge(self.paths)
        self.assertEqual(bridge.notify({"decision_id": "dec-1"})["status"], "disabled")
        bridge = NotificationBridge(self.paths, {"enabled": True, "command": "/definitely/missing"})
        result = bridge.notify({"decision_id": "dec-1"})
        self.assertEqual(result["status"], "unknown")

    def test_malformed_result_is_not_treated_as_answered(self):
        request = self.mailbox.raise_decision(
            run_id="run-1", node_id="issue-1", question="Pick", options=["A", "B"]
        )
        result_path = self.root / ".vibe" / "decisions" / "results" / (request["decision_id"] + ".json")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({"choice": "A"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.mailbox.list_pending("run-1")

    def test_result_validation_rejects_stale_digest_and_invalid_choice(self):
        request = self.mailbox.raise_decision(
            run_id="run-1", node_id="issue-1", question="Pick", options=["A", "B"]
        )
        result_path = self.root / ".vibe" / "decisions" / "results" / (request["decision_id"] + ".json")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({
                "schema_version": 1,
                "decision_id": request["decision_id"],
                "request_digest": "stale",
                "choice": "C",
                "answered_at": 1.0,
                "answered_via": "test",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            self.mailbox.result(request["decision_id"])

    def test_worker_originated_decision_uses_configured_notification_bridge(self):
        self.write_bridge({"enabled": True}, {"command": "/usr/bin/true", "timeout_seconds": 10})
        with patch("vibe_guide.decision_boundary.subprocess.run") as run:
            run.return_value.returncode = 0
            records, notifications = ingest_worker_decisions(
                self.paths,
                "run-1",
                "issue-1",
                [{"question": "Pick", "options": ["A", "B"]}],
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(notifications, [{"status": "accepted", "evidence": "notify_bridge:exit0"}])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/true")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_project_config_cannot_name_the_bridge_command(self):
        # Integrated PRD N8 / AC-D6: a cloned repository must not make this
        # machine run a program.
        self.write_bridge({"enabled": True, "command": "/usr/bin/true"},
                          {"command": "/usr/bin/true"})
        with patch("vibe_guide.decision_boundary.subprocess.run") as run:
            _records, notifications = ingest_worker_decisions(
                self.paths, "run-1", "issue-1",
                [{"question": "Pick", "options": ["A", "B"]}],
            )
        run.assert_not_called()
        self.assertEqual(notifications[0]["status"], "unknown")
        self.assertIn("user-level", notifications[0]["reason"])

    def test_user_command_needs_project_switch(self):
        self.write_bridge({"enabled": False}, {"command": "/usr/bin/true"})
        self.assertEqual(NotificationBridge(self.paths).notify({"decision_id": "dec-1"})["status"], "disabled")
        (self.root / ".vibe" / "config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(NotificationBridge(self.paths).notify({"decision_id": "dec-1"})["status"], "disabled")

    def test_relative_user_command_never_runs_a_repository_file(self):
        self.write_bridge({"enabled": True}, {"command": "./bridge"})
        with patch("vibe_guide.decision_boundary.subprocess.run") as run:
            _records, notifications = ingest_worker_decisions(
                self.paths, "run-1", "issue-1",
                [{"question": "Pick", "options": ["A", "B"]}],
            )
        run.assert_not_called()
        self.assertIn("absolute", notifications[0]["reason"])

    def test_symlinked_user_config_is_unknown_and_decision_still_lands(self):
        outside = Path(tempfile.mkdtemp()) / "config.json"
        outside.write_text(json.dumps({"notify_bridge": {"command": "/usr/bin/true"}}), encoding="utf-8")
        self.write_bridge({"enabled": True})
        (self.home / "config.json").symlink_to(outside)
        records, notifications = ingest_worker_decisions(
            self.paths, "run-1", "issue-1",
            [{"question": "Pick", "options": ["A", "B"]}],
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(notifications[0]["status"], "unknown")
        self.assertEqual(len(self.mailbox.list_pending("run-1")), 1)

    def test_symlinked_user_config_does_not_affect_projects_without_bridge(self):
        outside = Path(tempfile.mkdtemp()) / "config.json"
        outside.write_text("{}", encoding="utf-8")
        (self.home / "config.json").symlink_to(outside)
        self.assertEqual(NotificationBridge(self.paths).notify({"decision_id": "dec-1"})["status"], "disabled")
        self.write_bridge({"enabled": False})
        self.assertEqual(NotificationBridge(self.paths).notify({"decision_id": "dec-1"})["status"], "disabled")

    def test_timeout_must_be_a_bounded_number(self):
        for value in (True, "Infinity", 1e309, 0, 301):
            with self.subTest(value=value):
                self.write_bridge({"enabled": True}, {"command": "/usr/bin/true", "timeout_seconds": value})
                request = self.mailbox.raise_decision(run_id="run-1", node_id="n", question="Q%s" % value, options=["A", "B"])
                with patch("vibe_guide.decision_boundary.subprocess.run") as run:
                    result = NotificationBridge(self.paths).notify(request)
                run.assert_not_called()
                self.assertEqual(result["status"], "unknown")

    def test_enabled_without_user_command_is_unknown(self):
        self.write_bridge({"enabled": True})
        result = NotificationBridge(self.paths).notify({"decision_id": "dec-1"})
        self.assertEqual(result["status"], "unknown")

    def test_cli_reports_notification_bridge_failure_as_unknown_while_request_stays_pending(self):
        (self.root / ".project-root").write_text("fixture\n", encoding="utf-8")
        self.write_bridge({"enabled": True}, {"command": "/definitely/missing"})

        result = run_cli(
            [
                "decision-raise", "--json", "--run-id", "run-1", "--node", "issue-1",
                "--question", "Pick", "--options", "A,B",
            ],
            self.root,
        )

        self.assertEqual(result.exit_code, UNKNOWN)
        self.assertEqual(result.payload["status"], "unknown")
        self.assertEqual(result.payload["notification"]["status"], "unknown")
        self.assertEqual(len(self.mailbox.list_pending("run-1")), 1)


if __name__ == "__main__":
    unittest.main()


class _FakeSnapshot:
    def __init__(self, statuses):
        self.nodes = {node: {"status": status} for node, status in statuses.items()}
        self.ready_set = []


def _patch_snapshot(statuses):
    return patch("vibe_guide.supervisor.load_snapshot", return_value=_FakeSnapshot(statuses))


_OPTS = [{"id": "A", "label": "A", "impact": "fast"}, {"id": "B", "label": "B", "impact": "safe"}]
_OPTS_V2 = [{"id": "A", "label": "A", "impact": "fast"}, {"id": "C", "label": "C", "impact": "new"}]


class BoundaryCategoryTests(unittest.TestCase):
    """Integrated PRD N4 / AC-B4: shared categories, references, digest-bound answers."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, {"VIBE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.paths = ProjectPaths.from_cwd(self.root)
        self.mailbox = DecisionMailbox(self.paths)

    def raise_one(self, node="n1", options=None, **kwargs):
        return self.mailbox.raise_decision(
            run_id="run-1", node_id=node, question="Use A or B?",
            options=options or _OPTS, recommendation="A", **kwargs
        )

    def hold(self, node="n1", category="product_acceptance", **kwargs):
        from vibe_guide.supervisor import set_supervisor_hold
        with _patch_snapshot({"n1": "running", "n2": "running"}):
            return set_supervisor_hold(
                self.paths, "run-1", node, "等用户拍板", boundary_category=category, **kwargs
            )

    def holds_file(self):
        return self.root / ".vibe" / "runs" / "run-1" / "supervisor-holds.json"

    # -- holds -------------------------------------------------------------
    def test_alias_product_design_is_stored_canonical(self):
        holds = self.hold(category="product_design", authorization_gap="需要选 A/B")
        self.assertEqual(holds["n1"]["category"], "product_acceptance")

    def test_alias_deploy_is_stored_external_authorization(self):
        holds = self.hold(category="deploy", authorization_gap="需发布授权")
        self.assertEqual(holds["n1"]["category"], "external_authorization")
        on_disk = json.loads(self.holds_file().read_text(encoding="utf-8"))
        self.assertEqual(on_disk["holds"]["n1"]["category"], "external_authorization")

    def test_illegal_hold_category_is_rejected_without_state(self):
        with self.assertRaises(ValueError) as caught:
            self.hold(category="foo", authorization_gap="x")
        self.assertIn("product_acceptance", str(caught.exception))
        self.assertFalse(self.holds_file().exists())

    def test_hold_without_reference_is_rejected_without_state(self):
        with self.assertRaisesRegex(ValueError, "--decision"):
            self.hold()
        self.assertFalse(self.holds_file().exists())

    def test_hold_with_both_references_is_rejected(self):
        record = self.raise_one()
        with self.assertRaises(ValueError):
            self.hold(decision_id=record["decision_id"], authorization_gap="x")
        self.assertFalse(self.holds_file().exists())

    def test_hold_with_unknown_decision_is_rejected(self):
        for bad in ("dec-missing", "not an id"):
            with self.assertRaisesRegex(ValueError, "not found"):
                self.hold(decision_id=bad)
        self.assertFalse(self.holds_file().exists())

    def test_hold_decision_must_match_node_run_and_category(self):
        other_node = self.raise_one(node="n2")
        with self.assertRaises(ValueError):
            self.hold(node="n1", decision_id=other_node["decision_id"])
        other_run = self.mailbox.raise_decision(
            run_id="run-2", node_id="n1", question="Q?", options=_OPTS)
        with self.assertRaises(ValueError):
            self.hold(node="n1", decision_id=other_run["decision_id"])
        mine = self.raise_one(node="n1")
        with self.assertRaises(ValueError):
            self.hold(node="n1", category="external_authorization", decision_id=mine["decision_id"])
        self.assertFalse(self.holds_file().exists())

    def test_hold_with_decision_reference_is_persisted(self):
        mine = self.raise_one(node="n1")
        holds = self.hold(category="product_design", decision_id=mine["decision_id"])
        self.assertEqual(holds["n1"]["decision_id"], mine["decision_id"])
        self.assertNotIn("authorization_gap", holds["n1"])

    def test_hold_with_authorization_gap_is_persisted(self):
        holds = self.hold(category="external_authorization", authorization_gap="  需要发布授权 ")
        self.assertEqual(holds["n1"]["authorization_gap"], "需要发布授权")
        self.assertNotIn("decision_id", holds["n1"])
        with self.assertRaises(ValueError):
            self.hold(category="external_authorization", authorization_gap="x" * 201)

    def test_legacy_hold_without_category_reads_as_product_acceptance(self):
        from vibe_guide.supervisor import supervisor_holds
        path = self.holds_file()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"holds": {
            "n1": {"reason": "旧", "held_at": 1.0, "statuses": {}},
            "n2": {"category": "product_design", "reason": "旧别名", "held_at": 1.0, "statuses": {}},
        }}), encoding="utf-8")
        holds = supervisor_holds(self.paths, "run-1")
        self.assertEqual(holds["n1"]["category"], "product_acceptance")
        self.assertEqual(holds["n1"]["category_source"], "legacy_default")
        self.assertEqual(holds["n2"]["category"], "product_acceptance")

    def test_validator_is_shared_and_lists_aliases(self):
        from vibe_guide import decision_boundary
        self.assertEqual(decision_boundary.BOUNDARY_CATEGORIES,
                         ("product_acceptance", "external_authorization"))
        self.assertEqual(decision_boundary.validate_boundary_category(" deploy "), "external_authorization")
        with self.assertRaises(ValueError) as caught:
            decision_boundary.validate_boundary_category("foo")
        self.assertIn("aliases", str(caught.exception))

    # -- decision category -------------------------------------------------
    def test_decision_category_defaults_and_aliases_are_canonical(self):
        self.assertEqual(self.raise_one()["category"], "product_acceptance")
        record = self.raise_one(node="n2", category="deploy")
        self.assertEqual(record["category"], "external_authorization")
        self.assertEqual(self.mailbox.list_pending("run-1")[1]["category"], "external_authorization")

    def test_illegal_decision_category_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.raise_one(category="foo")
        self.assertFalse((self.root / ".vibe" / "decisions" / "requests").exists())

    def test_legacy_decision_without_category_loads_as_product_acceptance(self):
        record = self.raise_one()
        path = self.root / ".vibe" / "decisions" / "requests" / (record["decision_id"] + ".json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["category"]
        raw["request_digest"] = self.mailbox._request_digest(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        pending = self.mailbox.list_pending("run-1")
        self.assertEqual(pending[0]["category"], "product_acceptance")

    # -- digest-bound answers ---------------------------------------------
    def test_answer_requires_digest(self):
        record = self.raise_one()
        with self.assertRaisesRegex(ValueError, "--digest"):
            self.mailbox.answer(record["decision_id"], "A")
        self.assertFalse((self.root / ".vibe" / "decisions" / "results").exists())

    def test_answer_with_wrong_digest_is_rejected(self):
        record = self.raise_one()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.mailbox.answer(record["decision_id"], "A", request_digest="0" * 64)
        self.assertIsNone(self.mailbox.result(record["decision_id"]))

    def test_cli_answer_without_digest_is_blocked(self):
        from vibe_guide.cli import BLOCKED
        record = self.raise_one()
        result = run_cli([
            "decision-answer", "--json", "--id", record["decision_id"], "--choice", "A",
        ], cwd=self.root)
        self.assertEqual(result.exit_code, BLOCKED)
        self.assertIn("--digest", result.payload["reason"])
        self.assertFalse((self.root / ".vibe" / "decisions" / "results").exists())

    def test_changed_options_void_old_answer_and_resurface(self):
        record = self.raise_one()
        old_digest = record["request_digest"]
        self.mailbox.answer(record["decision_id"], "A", request_digest=old_digest)
        self.assertEqual(self.mailbox.list_pending("run-1"), [])
        again = self.raise_one(options=_OPTS_V2)
        self.assertEqual(again["decision_id"], record["decision_id"])
        self.assertNotEqual(again["request_digest"], old_digest)
        pending = self.mailbox.list_pending("run-1")
        self.assertEqual([item["decision_id"] for item in pending], [record["decision_id"]])
        self.assertEqual([o["id"] for o in pending[0]["options"]], ["A", "C"])
        archived = list((self.root / ".vibe" / "decisions" / "results" / "superseded").glob("*.json"))
        self.assertEqual(len(archived), 1)
        self.assertIn(old_digest[:16], archived[0].name)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.mailbox.answer(record["decision_id"], "A", request_digest=old_digest)
        self.mailbox.answer(record["decision_id"], "C", request_digest=again["request_digest"])
        self.assertEqual(self.mailbox.result(record["decision_id"])["choice"], "C")

    def test_identical_reraise_is_idempotent(self):
        first = self.raise_one(evidence={"source": "a"})
        second = self.raise_one(evidence={"source": "b"})
        self.assertEqual(first, second)
        self.assertEqual(second["evidence"], {"source": "a"})
        self.assertEqual(len(list((self.root / ".vibe" / "decisions" / "requests").glob("*.json"))), 1)

    # -- round-1 rework: all-or-nothing batches; tampered categories fail closed --
    def _requests_files(self):
        directory = self.root / ".vibe" / "decisions" / "requests"
        return list(directory.glob("*.json")) if directory.exists() else []

    def test_batch_with_illegal_category_in_second_item_writes_nothing(self):
        good = {"question": "Q1?", "options": _OPTS}
        bad = {"question": "Q2?", "options": _OPTS, "category": "foo"}
        with self.assertRaises(ValueError):
            self.mailbox.ingest_worker_requests("run-1", "n1", [good, bad])
        self.assertEqual(self._requests_files(), [])

    def test_batch_with_invalid_options_in_second_item_writes_nothing(self):
        good = {"question": "Q1?", "options": _OPTS}
        bad = {"question": "Q2?", "options": ["only-one"]}
        with self.assertRaises(ValueError):
            self.mailbox.ingest_worker_requests("run-1", "n1", [good, bad])
        self.assertEqual(self._requests_files(), [])

    def test_valid_batch_still_writes_every_item(self):
        records = self.mailbox.ingest_worker_requests("run-1", "n1", [
            {"question": "Q1?", "options": _OPTS},
            {"question": "Q2?", "options": _OPTS, "category": "deploy"},
        ])
        self.assertEqual(len(self._requests_files()), 2)
        self.assertEqual(records[1]["category"], "external_authorization")

    def _write_hold_category(self, value):
        path = self.holds_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"holds": {"n1": {
            "category": value, "reason": "r", "held_at": 1.0, "statuses": {"n1": "running"},
        }}}), encoding="utf-8")

    def test_alias_on_disk_reads_canonical(self):
        from vibe_guide.supervisor import supervisor_holds
        self._write_hold_category("deploy")
        self.assertEqual(supervisor_holds(self.paths, "run-1")["n1"]["category"], "external_authorization")

    def test_tampered_category_fails_closed(self):
        from vibe_guide.supervisor import supervisor_holds
        for value in ("TAMPERED", "", 123):
            with self.subTest(value=value):
                self._write_hold_category(value)
                with self.assertRaisesRegex(ValueError, "malformed"):
                    supervisor_holds(self.paths, "run-1")

    def test_tampered_category_makes_preflight_unknown(self):
        from vibe_guide.supervisor import supervisor_preflight
        self._write_hold_category("TAMPERED")
        record = self.root / "session.json"
        record.write_text(json.dumps({"token_count": 30000}), encoding="utf-8")
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(self.paths, "run-1", str(record))
        self.assertEqual(result["state"], "unknown", result)
