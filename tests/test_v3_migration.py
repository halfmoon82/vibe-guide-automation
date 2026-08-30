import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.diagnostics import diagnose_legacy_plan
import vibe_guide.migration as migration_module
from vibe_guide.migration import (
    inspect_legacy_plan,
    load_migration_report,
    migrate_legacy_plan,
)


class V3MigrationTests(unittest.TestCase):
    def _legacy_plan(self, root: Path) -> Path:
        plan = root / "v2-plan"
        plan.mkdir()
        (plan / "plan.json").write_text(
            json.dumps({"plan_id": "legacy", "version": 2, "status": "draft"}),
            encoding="utf-8",
        )
        (plan / "authorization-card.json").write_text(
            json.dumps(
                {
                    "plan_id": "legacy",
                    "plan_version": 2,
                    "digest": "old-digest",
                    "status": "confirmed",
                }
            ),
            encoding="utf-8",
        )
        (plan / "events.jsonl").write_text('{"event":"old"}\n', encoding="utf-8")
        return plan

    def test_missing_audit_is_retained_and_routed_to_new_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            original_card = (plan / "authorization-card.json").read_bytes()

            report = inspect_legacy_plan(plan)

            self.assertEqual(report.status, "planning_required")
            self.assertIn("dag-audit", report.diagnostic.missing)
            self.assertEqual(report.old_revision, 2)
            self.assertEqual(report.new_revision, 3)
            self.assertTrue(report.superseded_marker["read_only"])
            self.assertIsNone(report.current_authorization_digest)

            destination = root / "revision-3"
            migrated = migrate_legacy_plan(plan, destination)

            self.assertEqual(migrated.status, "planning_required")
            self.assertEqual((plan / "authorization-card.json").read_bytes(), original_card)
            self.assertTrue((destination / "superseded.json").is_file())
            self.assertEqual(
                json.loads((destination / "plan.json").read_text(encoding="utf-8"))["status"],
                "planning_required",
            )
            self.assertNotIn("old-digest", (destination / "plan.json").read_text(encoding="utf-8"))

    def test_ambiguous_lineage_is_blocked_unknown_with_remediation(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self._legacy_plan(Path(directory))
            (plan / "plan.json").write_text(json.dumps({"status": "draft"}), encoding="utf-8")

            diagnostic = diagnose_legacy_plan(plan)

            self.assertEqual(diagnostic.status, "blocked_unknown")
            self.assertTrue(diagnostic.remediation)

    def test_complete_legacy_evidence_still_cannot_be_current_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            (plan / "dag-audit.json").write_text(
                json.dumps({"status": "reviewed", "plan_id": "legacy"}), encoding="utf-8"
            )
            (plan / "plan-confirmation.json").write_text(
                json.dumps(
                    {
                        "status": "confirmed",
                        "plan_id": "legacy",
                        "plan_revision": 2,
                        "authorization_digest": "old-digest",
                    }
                ),
                encoding="utf-8",
            )

            report = inspect_legacy_plan(plan)
            self.assertEqual(report.status, "planning_required")
            self.assertIsNone(report.current_authorization_digest)
            self.assertIn("authorization-card.json", report.preserved_evidence)
            self.assertIn("events.jsonl", report.preserved_evidence)

    def test_migration_report_round_trips_and_nested_authorization_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            (plan / "plan.json").write_text(
                json.dumps(
                    {
                        "plan_id": "legacy",
                        "version": 2,
                        "status": "draft",
                        "authorization": {"digest": "old-digest"},
                        "metadata": {"confirmation": {"authorization_digest": "old-digest"}},
                    }
                ),
                encoding="utf-8",
            )
            target = root / "revision-3"
            report = migrate_legacy_plan(plan, target)

            self.assertEqual(load_migration_report(target / "migration-report.json").to_dict(), report.to_dict())
            self.assertNotIn("old-digest", (target / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(
                (target / "legacy-evidence" / "events.jsonl").read_text(encoding="utf-8"),
                '{"event":"old"}\n',
            )

    def test_route_binding_gap_is_reported_and_source_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            (plan / "nodes.json").write_text(
                json.dumps([{"id": "N1", "contract": {"provider": "codex-app-visible"}}]),
                encoding="utf-8",
            )
            before = (plan / "nodes.json").read_bytes()

            report = inspect_legacy_plan(plan)

            self.assertEqual(report.status, "planning_required")
            self.assertIn("route:N1:adapter_id", report.diagnostic.missing)
            self.assertIn("route:N1:worktree", report.diagnostic.missing)
            self.assertEqual((plan / "nodes.json").read_bytes(), before)

    def test_malformed_lineage_artifact_is_blocked_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self._legacy_plan(Path(directory))
            (plan / "authorization-card.json").write_text("not-json", encoding="utf-8")

            diagnostic = diagnose_legacy_plan(plan)

            self.assertEqual(diagnostic.status, "blocked_unknown")
            self.assertEqual(diagnostic.missing, ["lineage"])

    def test_directory_swap_during_migration_cannot_escape_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            (plan / "nested").mkdir()
            (plan / "nested" / "x").write_bytes(b"payload")
            outside = root / "outside"
            outside.mkdir()
            destination = root / "revision-3"
            original_replace = os.replace

            def swap_then_replace(src, dst, **kwargs):
                nested = destination / "legacy-evidence" / "nested"
                if nested.is_dir() and not nested.is_symlink():
                    nested.rename(destination / "legacy-evidence" / "detached")
                    nested.symlink_to(outside, target_is_directory=True)
                return original_replace(src, dst, **kwargs)

            with mock.patch("os.replace", side_effect=swap_then_replace):
                migrate_legacy_plan(plan, destination)

            self.assertEqual(list(outside.iterdir()), [])

    def test_nested_swap_before_migration_directory_open_cannot_escape_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            (plan / "nested").mkdir()
            (plan / "nested" / "x").write_bytes(b"payload")
            outside = root / "outside"
            outside.mkdir()
            destination = root / "revision-3"
            (destination / "legacy-evidence" / "nested").mkdir(parents=True)
            original_open = migration_module._open_relative_directory
            swapped = False

            def swap_before_open(parent_fd, parts):
                nonlocal swapped
                if tuple(parts) == ("nested",) and not swapped:
                    swapped = True
                    nested = destination / "legacy-evidence" / "nested"
                    nested.rename(destination / "legacy-evidence" / "detached")
                    nested.symlink_to(outside, target_is_directory=True)
                return original_open(parent_fd, parts)

            with mock.patch.object(
                migration_module,
                "_open_relative_directory",
                side_effect=swap_before_open,
            ):
                with self.assertRaises((ValueError, OSError)):
                    migrate_legacy_plan(plan, destination)

            self.assertEqual(list(outside.iterdir()), [])

    def test_root_swap_after_validation_cannot_write_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            outside = root / "outside"
            outside.mkdir()
            destination = root / "revision-3"
            original_open = migration_module._open_confined_directory
            swapped = False

            def open_then_swap(path):
                nonlocal swapped
                descriptor = original_open(path)
                if Path(path) == destination and not swapped:
                    swapped = True
                    destination.rename(root / "detached")
                    destination.symlink_to(outside, target_is_directory=True)
                return descriptor

            with mock.patch.object(migration_module, "_open_confined_directory", side_effect=open_then_swap):
                with self.assertRaises((ValueError, OSError)):
                    migrate_legacy_plan(plan, destination)

            self.assertEqual(list(outside.iterdir()), [])

    def test_new_plan_removes_all_legacy_lineage_keys_and_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            old_values = {
                "authorization_card_digest": "legacy-card-digest",
                "plan_confirmation_digest": "legacy-confirmation-digest",
                "dag_audit_digest": "legacy-audit-digest",
                "plan_binding_digest": "legacy-binding-digest",
                "node_contract_digest": "legacy-node-digest",
                "decision_digest": "legacy-decision-digest",
                "authorization_status": "legacy-authorized",
                "auth_digest": "legacy-auth-digest",
            }
            metadata = dict(old_values)
            metadata["opaque_old_value"] = "legacy-card-digest"
            (plan / "plan.json").write_text(
                json.dumps({"plan_id": "legacy", "version": 2, "status": "draft", "metadata": metadata}),
                encoding="utf-8",
            )
            destination = root / "revision-3"

            migrate_legacy_plan(plan, destination)

            new_plan = json.loads((destination / "plan.json").read_text(encoding="utf-8"))
            encoded = json.dumps(new_plan, ensure_ascii=False, sort_keys=True)
            for key, value in old_values.items():
                self.assertNotIn(key, encoded)
                self.assertNotIn(value, encoded)
            self.assertNotIn("legacy-card-digest", encoded)

    def test_new_plan_scrubs_nested_camelcase_lineage_and_revision_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            old_values = {
                "authorizationCard": "AUTHORIZATION_CARD_OLD",
                "authCard": "AUTH_CARD_OLD",
                "planConfirmation": "PLAN_CONFIRMATION_OLD",
                "dagAudit": "DAG_AUDIT_OLD",
                "planBinding": "PLAN_BINDING_OLD",
                "isAuthorized": True,
                "authorizedFlag": True,
                "decisionDigest": "DECISION_DIGEST_OLD",
            }
            payload = {
                "plan_id": "legacy",
                "version": 3,
                "plan_version": 3,
                "plan_revision": 2,
                "planRevision": 2,
                "revision": 2,
                "metadata": {
                    "nested": {
                        "lineage": old_values,
                        "opaqueLegacyValue": "AUTHORIZATION_CARD_OLD",
                        "revisionAlias": 2,
                        "currentRevision": 2,
                        "versionAlias": 2,
                    },
                    "safe": {"label": "keep-me", "enabled": True},
                },
            }
            (plan / "plan.json").write_text(json.dumps(payload), encoding="utf-8")
            (plan / "authorization-card.json").write_text(
                json.dumps(
                    {
                        "plan_id": "legacy",
                        "plan_version": 3,
                        "digest": "AUTHORIZATION_CARD_OLD",
                    }
                ),
                encoding="utf-8",
            )
            target = root / "revision-4"

            report = migrate_legacy_plan(plan, target)
            new_plan = json.loads((target / "plan.json").read_text(encoding="utf-8"))

            lineage_terms = (
                "authorization",
                "auth",
                "card",
                "confirmation",
                "audit",
                "binding",
                "decision",
                "authorized",
            )
            revision_aliases = {
                "version",
                "planversion",
                "planrevision",
                "revision",
                "revisionnumber",
                "planrevisionnumber",
                "planrev",
                "rev",
            }

            def walk(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        compact = "".join(ch for ch in str(key).casefold() if ch.isalnum())
                        yield compact, item
                        yield from walk(item)
                elif isinstance(value, list):
                    for item in value:
                        yield from walk(item)

            for compact, item in walk(new_plan):
                if any(term in compact for term in lineage_terms):
                    self.fail("legacy lineage key retained: {}".format(compact))
                if compact in revision_aliases:
                    self.assertEqual(item, report.new_revision)
            encoded = json.dumps(new_plan, ensure_ascii=False, sort_keys=True)
            for old_value in old_values.values():
                self.assertNotIn(str(old_value), encoded)
            self.assertNotIn("AUTHORIZATION_CARD_OLD", encoded)
            self.assertEqual(new_plan["version"], report.new_revision)
            self.assertEqual(new_plan["plan_version"], report.new_revision)
            self.assertEqual(new_plan["metadata"]["safe"]["label"], "keep-me")
            self.assertTrue(new_plan["metadata"]["safe"]["enabled"])

    def test_new_plan_scrubs_lineage_list_scalars_repeated_in_ordinary_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            repeated_digest = "LIST_OLD_AUTH_DIGEST"
            payload = {
                "plan_id": "legacy",
                "version": 2,
                "metadata": {
                    "authorization": {
                        "digestHistory": [repeated_digest, "older-value"],
                    },
                    "ordinary": {
                        "opaqueReference": repeated_digest,
                    },
                },
            }
            (plan / "plan.json").write_text(json.dumps(payload), encoding="utf-8")
            target = root / "revision-3"

            migrate_legacy_plan(plan, target)
            encoded = (target / "plan.json").read_text(encoding="utf-8")

            self.assertNotIn(repeated_digest, encoded)
            self.assertNotIn("digestHistory", encoded)

    def test_new_plan_scrubs_tuple_and_nested_tuple_lineage_scalars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            payload = {
                "plan_id": "legacy",
                "version": 2,
                "authorization": {
                    "digestHistory": (
                        "TUPLE_OLD_DIGEST",
                        ("NESTED_TUPLE_OLD_DIGEST", "nested-safe"),
                    )
                },
                "metadata": {
                    "opaqueTupleDigest": "TUPLE_OLD_DIGEST",
                    "opaqueNestedDigest": "NESTED_TUPLE_OLD_DIGEST",
                    "safe": True,
                },
            }
            (plan / "plan.json").write_text(
                json.dumps({"plan_id": "legacy", "version": 2, "status": "draft"}),
                encoding="utf-8",
            )
            target = root / "revision-3"

            with mock.patch.object(migration_module, "_read_plan", return_value=payload):
                migrate_legacy_plan(plan, target)
            new_plan = json.loads((target / "plan.json").read_text(encoding="utf-8"))
            encoded = json.dumps(new_plan, ensure_ascii=False, sort_keys=True)

            self.assertNotIn("TUPLE_OLD_DIGEST", encoded)
            self.assertNotIn("NESTED_TUPLE_OLD_DIGEST", encoded)
            self.assertNotIn("digestHistory", encoded)
            self.assertTrue(new_plan["metadata"]["safe"])

    def test_contradictory_route_fields_are_blocked_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            (plan / "nodes.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "N1",
                            "adapter_id": "codex",
                            "provider": "codex-app-visible",
                            "mode": "visible",
                            "project": "p",
                            "host": "h",
                            "worktree": "wt",
                            "branch": "b",
                            "allowlist": ["safe.py"],
                            "route": {"provider": "other-provider"},
                        }
                    ]
                ),
                encoding="utf-8",
            )

            diagnostic = diagnose_legacy_plan(plan)

            self.assertEqual(diagnostic.status, "blocked_unknown")
            self.assertTrue(any("conflict" in item for item in diagnostic.missing))
            self.assertTrue(diagnostic.remediation)

    def test_malformed_route_types_and_nodes_are_blocked_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            (plan / "nodes.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "N1",
                            "adapter_id": "codex",
                            "provider": "codex-app-visible",
                            "mode": True,
                            "project": "p",
                            "host": "h",
                            "worktree": "wt",
                            "branch": "b",
                            "allowlist": {},
                        }
                    ]
                ),
                encoding="utf-8",
            )

            diagnostic = diagnose_legacy_plan(plan)

            self.assertEqual(diagnostic.status, "blocked_unknown")
            self.assertTrue(any("invalid" in item for item in diagnostic.missing))

    def test_empty_or_invalid_nodes_artifact_is_blocked_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._legacy_plan(root)
            (plan / "nodes.json").write_text("[]", encoding="utf-8")

            diagnostic = diagnose_legacy_plan(plan)

            self.assertEqual(diagnostic.status, "blocked_unknown")
            self.assertIn("route:nodes:invalid", diagnostic.missing)


if __name__ == "__main__":
    unittest.main()
