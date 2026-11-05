"""P2 ``local_commit_allowed``: a signed local-commit exception inside ``deny``.

The V4.5 design folds commit/push/PR/MR/merge into one ``remote_git_actions``
group: ``allow`` grants the whole group, ``deny`` grants none -- including
local commits.  r4 adds a narrow, explicitly signed exception: a card may
record ``local_commit_allowed: true`` *only* beside ``remote_git_actions:
deny``.  Absent or ``false`` is bit-for-bit the old meaning; ``allow`` + the
flag is refused so two permission sources never coexist.

These tests exercise the rules without touching the protected run:

* the flag is bound into the card digest only when true (B07/B09);
* deny+exception authorizes whitelist-only local commits and nothing remote
  (B08/B11);
* deny without the exception no longer tells the worker to commit locally
  (B10);
* an executor that does not know the flag fails closed on the new card
  (B09).
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import vibe_guide
from vibe_guide.authorization import (
    _canonical_digest,
    _REMOTE_GIT_ACTIONS_SCOPE,
    AuthorizationCard,
    AuthorizationRecord,
    authorize,
    build_authorization_card,
    is_authorization_integrity_valid,
    refresh_authorization_card,
    remote_git_actions_allowed,
    validate_authorization_card_consistency,
    worker_push_ban_text,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


FROZEN_HEAD = "885236783e1a680d7165ee2ee2c4e0002430a30a"


def setUpModule():
    import subprocess
    root = Path(vibe_guide.__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        head = "unreadable"
    print("[loaded vibe_guide: {} version={} head={} frozen={}]".format(
        vibe_guide.__file__, getattr(vibe_guide, "__version__", "?"),
        head, FROZEN_HEAD))


def _frozen_head():
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(vibe_guide.__file__).resolve().parents[1]),
             "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception:  # pragma: no cover - diagnostics only
        return "unreadable"


CAPS = AgentCapabilities("claude-code", True, True, True, False, True, "full")


def _plan():
    node = DAGNode(
        "n1", "N1", [], [], None,
        {"input": "i", "output": "o", "error_behavior": "e",
         "acceptance_example": "a", "adapter_id": "claude-code",
         "files": ["a.py"]},
        "planned",
    )
    plan = Plan("p1", 1, ".vibe/plans/p1/prd.md", ["n1"],
                "confirmed_pending_authorization",
                authorization_required=True, nodes=[node])
    return plan, [node]


class SignedFieldTests(unittest.TestCase):
    """The flag enters the signed payload only when true (B07/B09)."""

    def test_absent_and_false_keep_legacy_digest(self):
        plan, nodes = _plan()
        legacy = build_authorization_card(plan, nodes, CAPS,
                                          remote_git_actions="deny")
        explicit_false = build_authorization_card(
            plan, nodes, CAPS, remote_git_actions="deny",
            local_commit_allowed=False)
        self.assertEqual(legacy.digest, explicit_false.digest)
        self.assertNotIn("local_commit_allowed", legacy.to_dict())

    def test_true_is_bound_into_card_and_record_digest(self):
        plan, nodes = _plan()
        card = build_authorization_card(
            plan, nodes, CAPS, remote_git_actions="deny",
            local_commit_allowed=True)
        self.assertTrue(validate_authorization_card_consistency(card))
        data = card.to_dict()
        self.assertTrue(data.get("local_commit_allowed") is True)
        record = authorize(card, "AUTHORIZE")
        self.assertTrue(is_authorization_integrity_valid(record))
        restored = AuthorizationRecord.from_dict(
            json.loads(json.dumps(record.to_dict())))
        self.assertTrue(restored.local_commit_allowed is True)
        self.assertEqual(restored.digest, card.digest)

    def test_allow_plus_true_is_refused(self):
        plan, nodes = _plan()
        with self.assertRaises(ValueError):
            build_authorization_card(
                plan, nodes, CAPS, remote_git_actions="allow",
                local_commit_allowed=True)
        # And a tampered card/record carrying the combination fails closed.
        card = build_authorization_card(plan, nodes, CAPS,
                                        remote_git_actions="allow")
        forged = card.to_dict()
        forged["local_commit_allowed"] = True
        with self.assertRaises((TypeError, ValueError)):
            AuthorizationCard(
                **{key: value for key, value in forged.items()
                   if key != "dependency_graph"})
        record = AuthorizationRecord.from_dict(
            json.loads(json.dumps(authorize(card, "AUTHORIZE").to_dict())))
        record_data = record.to_dict()
        record_data["remote_git_actions"] = "allow"
        record_data["local_commit_allowed"] = True
        record_data.pop("remote_git_actions_options", None)
        record_data.pop("remote_git_actions_scope", None)
        record_data.pop("deploy_authorization", None)
        with self.assertRaises(ValueError):
            AuthorizationRecord.from_dict(record_data)

    def test_wrong_type_and_tampered_digest_refused(self):
        plan, nodes = _plan()
        with self.assertRaises((TypeError, ValueError)):
            build_authorization_card(
                plan, nodes, CAPS, remote_git_actions="deny",
                local_commit_allowed="yes")
        card = build_authorization_card(
            plan, nodes, CAPS, remote_git_actions="deny",
            local_commit_allowed=True)
        record = AuthorizationRecord.from_dict(
            json.loads(json.dumps(authorize(card, "AUTHORIZE").to_dict())))
        data = record.to_dict()
        data["local_commit_allowed"] = False
        data.pop("remote_git_actions_options", None)
        data.pop("remote_git_actions_scope", None)
        data.pop("deploy_authorization", None)
        restored = AuthorizationRecord.from_dict(data)
        # The field is signed: unilateral flip invalidates integrity.
        self.assertFalse(is_authorization_integrity_valid(restored))

    def test_legacy_records_and_cards_round_trip_unchanged(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS,
                                        remote_git_actions="deny")
        record = authorize(card, "AUTHORIZE")
        restored = AuthorizationRecord.from_dict(
            json.loads(json.dumps(record.to_dict())))
        self.assertTrue(is_authorization_integrity_valid(restored))
        self.assertFalse(bool(getattr(restored, "local_commit_allowed", False)))
        # Refresh keeps the denied, no-exception meaning bit-for-bit.
        refreshed = refresh_authorization_card(plan, nodes, card)
        self.assertEqual(refreshed.digest, card.digest)

    def test_unknown_field_still_refused_forwards(self):
        # An executor that does not know a field refuses the card rather than
        # ignoring it (B09): unknown keys never reach the dataclass.
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS,
                                        remote_git_actions="deny")
        data = card.to_dict()
        data["local_commit_allowed"] = True
        data["unrelated_new_field"] = 1
        with self.assertRaises(TypeError):
            AuthorizationCard(
                **{key: value for key, value in data.items()
                   if key not in {"dependency_graph", "platform_capabilities",
                                  "model_tiers"}})


class RemoteDenialTests(unittest.TestCase):
    """deny+exception never re-opens anything remote (B08)."""

    def test_exception_grants_no_remote_verb(self):
        plan, nodes = _plan()
        card = build_authorization_card(
            plan, nodes, CAPS, remote_git_actions="deny",
            local_commit_allowed=True)
        for action in ("commit", "push", "create_pr", "create_mr", "merge",
                       "pr", "mr", "merge_local", "merge_remote",
                       "create_change_request", "deploy"):
            self.assertFalse(
                remote_git_actions_allowed(card, action), action)
        self.assertFalse(
            set(card.allowed_actions) & set(_REMOTE_GIT_ACTIONS_SCOPE),
            card.allowed_actions)

    def test_allow_card_still_grants_remote_group(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS,
                                        remote_git_actions="allow")
        for action in ("commit", "push", "create_pr", "merge"):
            self.assertTrue(remote_git_actions_allowed(card, action), action)


class PromptTextTests(unittest.TestCase):
    """The prompt must not promise commits a plain deny forbids (B10)."""

    def test_deny_without_exception_forbids_commit(self):
        text = worker_push_ban_text(None, {"remote_git_actions": "deny"})
        self.assertNotIn("只在本地分支提交", text)
        self.assertIn("提交", text)

    def test_deny_with_exception_scopes_commit_to_whitelist(self):
        text = worker_push_ban_text(
            None, {"remote_git_actions": "deny", "local_commit_allowed": True})
        self.assertIn("提交", text)
        # Still bans every remote verb.
        for verb in ("push", "PR", "merge"):
            self.assertIn(verb, text)


class NodeSpecFieldTests(unittest.TestCase):
    """``local_commit_allowed`` is a business choice the PM may write (B10)."""

    def test_product_spec_fields_declares_the_choice(self):
        from vibe_guide.node_spec import PRODUCT_SPEC_FIELDS
        self.assertIn("local_commit_allowed?", PRODUCT_SPEC_FIELDS)

    def test_choice_is_not_an_engineering_field(self):
        from vibe_guide.node_spec import reject_engineering_fields
        reject_engineering_fields({
            "title": "t", "objective": "o", "local_commit_allowed": True,
            "nodes": []})


FROZEN_HEAD = "885236783e1a680d7165ee2ee2c4e0002430a30a"


class RealGitCommitGateTests(unittest.TestCase):
    """B11: real local Git checks for the whitelist commit path.

    The gate lives beside the authorization logic in ``authorization.py`` so
    the monitor, the CLI and these tests share one source of truth.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lc-gate-"))
        self.addCleanup(self._cleanup)
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.root / "a.py").write_text("a\n", encoding="utf-8")
        self._git("add", "a.py")
        self._git("commit", "-qm", "base")
        self.base = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("checkout", "-qb", "node/n1")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.root)] + list(args),
                              capture_output=True, text=True, timeout=30)

    def _commit_file(self, name, content="x\n"):
        (self.root / name).write_text(content, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-qm", "add " + name)

    def test_whitelist_commit_lands(self):
        self._commit_file("a.py", "a2\n")
        from vibe_guide.authorization import commit_landing_gate
        result = commit_landing_gate(
            worktree=self.root, expected_branch="node/n1",
            base=self.base, file_scope={"a.py"},
            local_commit_allowed=True)
        self.assertEqual(result["status"], "ok", result)

    def test_file_outside_whitelist_rejected(self):
        self._commit_file("b.py")
        from vibe_guide.authorization import commit_landing_gate
        result = commit_landing_gate(
            worktree=self.root, expected_branch="node/n1",
            base=self.base, file_scope={"a.py"},
            local_commit_allowed=True)
        self.assertEqual(result["status"], "rejected", result)
        self.assertIn("b.py", json.dumps(result))

    def test_wrong_branch_rejected(self):
        self._git("checkout", "-qb", "node/other")
        self._commit_file("a.py", "a3\n")
        from vibe_guide.authorization import commit_landing_gate
        result = commit_landing_gate(
            worktree=self.root, expected_branch="node/n1",
            base=self.base, file_scope={"a.py"},
            local_commit_allowed=True)
        self.assertEqual(result["status"], "rejected", result)

    def test_no_exception_rejects_landed_commits(self):
        self._commit_file("a.py", "a4\n")
        from vibe_guide.authorization import commit_landing_gate
        result = commit_landing_gate(
            worktree=self.root, expected_branch="node/n1",
            base=self.base, file_scope={"a.py"},
            local_commit_allowed=False)
        self.assertEqual(result["status"], "rejected", result)

    def test_unreadable_git_state_is_unknown_not_clean(self):
        from vibe_guide.authorization import commit_landing_gate
        result = commit_landing_gate(
            worktree=self.root / "missing", expected_branch="node/n1",
            base=self.base, file_scope={"a.py"},
            local_commit_allowed=True)
        self.assertEqual(result["status"], "unknown", result)


if __name__ == "__main__":
    unittest.main()
