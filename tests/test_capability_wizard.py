import unittest

from vibe_guide.capability_contract import (
    CapabilityAuthorization,
    CapabilityItem,
)
from vibe_guide.capability_wizard import (
    authorize_capabilities,
    build_capability_catalog,
)


class CapabilityWizardTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "capabilities": [
                {"name": "filesystem", "scope": "local", "status": "verified_available"},
                {"name": "python", "scope": "local", "status": "verified_available", "required": False},
                {"name": "credentials", "scope": "external", "status": "unknown"},
                {"name": "system_permissions", "scope": "external", "status": "permission_denied"},
                {"name": "platform_login", "scope": "external", "status": "unknown_timeout"},
                {"name": "Deploy", "scope": "external", "status": "verified_available"},
            ]
        }

    def test_catalog_has_three_layers_and_preserves_scan_status(self):
        catalog = build_capability_catalog(self.report)
        by_name = {item.name: item for item in catalog}
        self.assertEqual(by_name["filesystem"].layer, "local_required")
        self.assertEqual(by_name["python"].layer, "optional_development")
        self.assertEqual(by_name["credentials"].layer, "sensitive_external")
        self.assertEqual(by_name["credentials"].status, "unknown")
        self.assertEqual(by_name["system_permissions"].status, "permission_denied")
        self.assertEqual(by_name["platform_login"].status, "unknown_timeout")

    def test_layered_requires_each_selection_and_keeps_sensitive_pending(self):
        result = authorize_capabilities("layered", {"filesystem": {"layer": "local_required", "status": "verified_available", "evidence_ref": "scan:f", "selected": True}, "python": {"layer": "optional_development", "status": "verified_available", "evidence_ref": "scan:p", "selected": True}, "credentials": {"layer": "sensitive_external", "status": "unknown", "evidence_ref": "scan:c", "selected": True}})
        self.assertIsInstance(result, CapabilityAuthorization)
        self.assertEqual(result.mode, "layered")
        self.assertEqual(result.granted, ("filesystem", "python"))
        self.assertIn("credentials", result.pending)
        self.assertNotIn("credentials", result.granted)

    def test_bundled_grants_required_and_only_checked_optional(self):
        result = authorize_capabilities("bundled", {"filesystem": {"layer": "local_required", "status": "verified_available", "evidence_ref": "scan:f", "selected": True}, "python": {"layer": "optional_development", "status": "verified_available", "evidence_ref": "scan:p", "selected": True}, "credentials": {"layer": "sensitive_external", "status": "unknown", "evidence_ref": "scan:c", "selected": True}, "Deploy": {"layer": "sensitive_external", "status": "verified_available", "evidence_ref": "scan:d", "selected": True}, "platform_login": {"layer": "sensitive_external", "status": "unknown_timeout", "evidence_ref": "scan:l", "selected": False}, "system_permissions": {"layer": "sensitive_external", "status": "permission_denied", "evidence_ref": "scan:s", "selected": False}})
        self.assertEqual(result.granted, ("filesystem", "python"))
        self.assertEqual(set(result.pending), {"credentials", "Deploy", "platform_login", "system_permissions"})

    def test_unknown_states_are_not_reclassified(self):
        catalog = build_capability_catalog(self.report)
        result = authorize_capabilities("bundled", {"python": False})
        statuses = {item.name: item.status for item in catalog}
        self.assertEqual(statuses["credentials"], "unknown")
        self.assertEqual(statuses["platform_login"], "unknown_timeout")
        self.assertNotIn("unavailable", statuses.values())

    def test_item_list_preserves_states_and_evidence_and_blocks_remote_sensitive(self):
        catalog = build_capability_catalog({"capabilities": [
            {"name": "remote_write", "scope": "external", "status": "verified_available", "evidence_ref": "probe:r"},
            {"name": "python", "scope": "local", "required": False, "status": "unknown_timeout", "evidence_ref": "probe:p"},
        ]})
        result = authorize_capabilities("layered", catalog)
        self.assertNotIn("remote_write", result.granted)
        self.assertIn("remote_write", result.pending)
        self.assertEqual(result.capability_states["python"], "unknown_timeout")
        self.assertEqual(result.evidence_refs["python"], "probe:p")

    def test_bundled_empty_scan_does_not_invent_filesystem(self):
        result = authorize_capabilities("bundled", [])
        self.assertEqual(result.granted, ())

    def test_authorization_serializes_states_and_evidence(self):
        catalog = build_capability_catalog({"capabilities": [
            {"name": "credentials", "scope": "external", "status": "permission_denied", "evidence_ref": "probe:c"},
        ]})
        data = authorize_capabilities("bundled", catalog).to_dict()
        self.assertEqual(data["capability_states"]["credentials"], "permission_denied")
        self.assertEqual(data["evidence_refs"]["credentials"], "probe:c")

    def test_unbound_bool_mapping_is_pending_without_invented_evidence(self):
        result = authorize_capabilities("bundled", {"python": True})
        self.assertEqual(result.granted, ())
        self.assertIn("python", result.pending)
        self.assertEqual(result.capability_states["python"], "unknown")
        self.assertNotIn("python", result.evidence_refs)

    def test_mapping_with_scan_metadata_can_authorize_only_verified_local(self):
        result = authorize_capabilities("bundled", {
            "filesystem": {"layer": "local_required", "status": "verified_available", "evidence_ref": "scan:f", "selected": False},
            "remote_merge": {"layer": "sensitive_external", "status": "verified_available", "evidence_ref": "scan:r", "selected": True},
        })
        self.assertEqual(result.granted, ("filesystem",))
        self.assertIn("remote_merge", result.pending)


if __name__ == "__main__":
    unittest.main()
