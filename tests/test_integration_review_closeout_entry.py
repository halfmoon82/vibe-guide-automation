"""The production entry point for run-level integration review evidence.

`evaluate_v41_closeout` requires `snapshot.integration_review_evidence`, but
until this module's fix nothing in production ever wrote it: a complex run
could accept every node, including `integration-review`, and still sit at
`running` forever with the reason "integration review evidence is missing".
The reviewer already reports through the §6.2 mailbox, so that acceptance is
the entry point -- vibe derives the run-bound fields and keeps only the
reviewer's judgement fields as input.

Every test here drives the public CLI and the real provider mailbox, or the
pure derivation used by it.  None hand-edits a `.vibe/` file.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide.cli import render_v41_closeout_status, run_cli
from vibe_guide.evidence import (
    INTEGRATION_REVIEW_CLAIM_KEYS,
    build_integration_review_evidence,
    evaluate_v41_closeout,
    validate_integration_review_evidence,
)
from vibe_guide import monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import load_protocol
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.state import durable_projection, load_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
REQUEST = "设计并实现保单查看页的 PDF 导出，集成日期范围筛选、编写测试并部署"
FACTS = {name: True for name in (
    "claude-code.agent", "claude-code.shell", "claude-code.subprocess", "claude-code.worktree",
    "claude-code.visible_task.create", "claude-code.visible_task.enter",
    "claude-code.visible_task.resume", "claude-code.visible_task.wait",
)}
CLEARED_CLAIM = {
    "findings": [],
    "iteration_compatibility": {"status": "verified", "evidence": "迭代兼容性已逐条复核"},
    "test_runtime_delivery": {"status": "verified", "evidence": "测试与运行时交付已复核"},
    "out_of_scope": [],
}


def _snapshot(nodes=None, evidence=None):
    """A lineage-complete snapshot stand-in for the pure derivation tests."""
    return SimpleNamespace(
        run_id="run-1", plan_id="plan-1", plan_version=2, status="running",
        authorization_digest="a" * 64, node_contract_digest="b" * 64,
        prd_digest="c" * 64, spec_digest="d" * 64,
        nodes=nodes if nodes is not None else {
            "alpha": {"status": "accepted"},
            "beta": {"status": "accepted"},
            "integration-review": {"status": "accepted"},
        },
        integration_review_evidence=evidence or {},
        authorization={"remote_git_actions": "deny"},
    )


class IntegrationReviewPackageDerivationTests(unittest.TestCase):
    """What the reviewer may say, and what only the run may say."""

    def build(self, claim, snapshot=None):
        return build_integration_review_evidence(
            snapshot or _snapshot(), claim,
            agentsmd_acceptance_refs=["AGENTS.md"],
            unverified_or_excluded=["deploy", "release"],
        )

    def test_cleared_claim_derives_a_package_the_validator_accepts(self):
        snapshot = _snapshot()
        package = self.build(CLEARED_CLAIM, snapshot)
        validate_integration_review_evidence(snapshot, package)
        self.assertEqual(package["clearance"], {"p0": 0, "p1": 0, "p2": 0})

    def test_lineage_and_scope_come_from_the_run_not_from_the_reviewer(self):
        snapshot = _snapshot()
        package = self.build(CLEARED_CLAIM, snapshot)
        self.assertEqual(package["run_id"], "run-1")
        self.assertEqual(package["plan_revision"], 2)
        self.assertEqual(package["authorization_digest"], "a" * 64)
        self.assertEqual(package["aggregated_scope"]["nodes"], ["alpha", "beta"])
        self.assertEqual(package["agentsmd_acceptance_refs"], ["AGENTS.md"])
        self.assertEqual(package["unverified_or_excluded"], ["deploy", "release"])

    def test_claim_schema_is_exact_so_a_reviewer_cannot_forge_a_derived_field(self):
        for claim in (
            dict(CLEARED_CLAIM, run_id="other-run"),
            dict(CLEARED_CLAIM, clearance={"p0": 0, "p1": 0, "p2": 0}),
            {key: value for key, value in CLEARED_CLAIM.items() if key != "findings"},
            "P0-P2 cleared",
            None,
        ):
            with self.assertRaises(ValueError, msg=claim):
                self.build(claim)

    def test_unresolved_findings_are_counted_into_the_clearance(self):
        claim = dict(CLEARED_CLAIM, findings=[
            {"severity": "p1", "status": "open", "detail": "边界未覆盖"},
            {"severity": "p2", "status": "resolved", "detail": "已修"},
        ])
        self.assertEqual(self.build(claim)["clearance"], {"p0": 0, "p1": 1, "p2": 0})

    def test_only_a_resolved_finding_clears_so_a_reviewer_cannot_waive_its_own_p0(self):
        """`waived`/`accepted` are not clearances the reviewer may grant itself.

        Both spellings were registered as legal statuses while only `open` was
        counted, so `{"severity": "p0", "status": "waived"}` derived an all-zero
        clearance and closed the run out -- a self-served waiver with no human
        authorization.  Only `resolved` may clear a finding.
        """
        for status in ("waived", "accepted", "open"):
            claim = dict(CLEARED_CLAIM, findings=[
                {"severity": "p0", "status": status, "detail": "资金结算路径没验证"},
            ])
            self.assertEqual(self.build(claim)["clearance"], {"p0": 1, "p1": 0, "p2": 0}, msg=status)

    def test_an_unregistered_finding_status_or_severity_fails_closed(self):
        for finding in (
            {"severity": "p0", "status": "unresolved", "detail": "x"},
            {"severity": "p0", "status": "pending", "detail": "x"},
            {"severity": "p0", "detail": "x"},
            {"severity": "critical", "status": "open", "detail": "x"},
            {"severity": "p3", "status": "open", "detail": "x"},
            {"status": "open", "detail": "x"},
            "p0 open",
        ):
            with self.assertRaises(ValueError, msg=finding):
                self.build(dict(CLEARED_CLAIM, findings=[finding]))

    def test_a_verdict_must_carry_text_the_disk_can_keep(self):
        """A nested object under `evidence` cannot survive persistence.

        `state.py` redacts any value whose key is `evidence`, and for a dict it
        drops the keys that look sensitive -- so `{"secret_scan": "clean"}`
        persisted as `{}`, which the reload-time validator rejects.  The run
        announced `complete` once and then silently rolled back to the previous
        snapshot.  Reject the shape instead.
        """
        for verdict in (
            {"status": "verified", "evidence": {"secret_scan": "clean"}},
            {"status": "verified", "evidence": ["checked"]},
            {"status": "verified", "evidence": True},
            {"status": "verified", "evidence": ""},
            {"status": "verified"},
        ):
            with self.assertRaises(ValueError, msg=verdict):
                self.build(dict(CLEARED_CLAIM, iteration_compatibility=verdict))
            with self.assertRaises(ValueError, msg=verdict):
                self.build(dict(CLEARED_CLAIM, test_runtime_delivery=verdict))

    def test_the_derived_package_still_validates_after_persistence_redacts_it(self):
        """What is written must be what can be read back.

        The durable copy keeps the lineage and the clearance but replaces every
        provider text with a placeholder, so the package has to stay valid under
        that projection or the completed run becomes unloadable.
        """
        snapshot = _snapshot()
        package = self.build(dict(CLEARED_CLAIM, findings=[
            {"severity": "p2", "status": "resolved", "detail": "已修"},
        ]), snapshot)
        validate_integration_review_evidence(snapshot, durable_projection(package))

    def test_out_of_scope_changes_are_rejected(self):
        with self.assertRaises(ValueError):
            self.build(dict(CLEARED_CLAIM, out_of_scope=["docs/unrelated.md"]))

    def test_an_undeclared_acceptance_reference_fails_closed(self):
        with self.assertRaises(ValueError):
            build_integration_review_evidence(
                _snapshot(), CLEARED_CLAIM,
                agentsmd_acceptance_refs=[], unverified_or_excluded=["deploy"],
            )


class CloseoutTextTests(unittest.TestCase):
    """The product-facing text must read the keys the package actually carries.

    `render_v41_closeout_status` used to look for `status` and `p0_p2`, two
    keys the validator's exact key set forbids, so the text stayed "未闭合"
    even for a run that had legitimately closed out.  Pin the reader to the
    decision instead of to a key name.
    """

    def valid_package(self, snapshot):
        return build_integration_review_evidence(
            snapshot, CLEARED_CLAIM,
            agentsmd_acceptance_refs=["AGENTS.md"], unverified_or_excluded=["deploy"],
        )

    def test_text_says_passed_exactly_when_the_closeout_allows_it(self):
        snapshot = _snapshot()
        snapshot.integration_review_evidence = self.valid_package(snapshot)
        snapshot.status = "complete"
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)
        self.assertEqual(render_v41_closeout_status(snapshot), "整合通过但外部动作未授权")
        snapshot.authorization = {"remote_git_actions": "allow"}
        self.assertEqual(render_v41_closeout_status(snapshot), "整合 Review 已通过")

    def test_text_withholds_acceptance_when_the_closeout_does_not_allow_it(self):
        snapshot = _snapshot()
        package = self.valid_package(snapshot)
        package["findings"] = [{"severity": "p0", "status": "open", "detail": "资金路径"}]
        package["clearance"] = {"p0": 1, "p1": 0, "p2": 0}
        snapshot.integration_review_evidence = package
        snapshot.status = "complete"
        self.assertFalse(evaluate_v41_closeout(snapshot).allowed)
        text = render_v41_closeout_status(snapshot)
        self.assertIn("未闭合", text)
        self.assertIn("不可验收", text)


class ProtocolDocumentsTheClaimTests(unittest.TestCase):
    """The host agent can only send the right shape if the protocol says it.

    Anchored to the one subsection that owns the fact, so a key added to
    `INTEGRATION_REVIEW_CLAIM_KEYS` without a protocol update turns this red
    instead of silently letting reviewers report an unaccepted shape.
    """

    SECTION_HEADING = "#### 整合审查节点的 accepted"

    def section(self):
        text = load_protocol("prd-guide")
        self.assertIn(self.SECTION_HEADING, text)
        return text.split(self.SECTION_HEADING, 1)[1].split("\n#", 1)[0]

    def test_every_claim_key_the_code_requires_is_documented(self):
        section = self.section()
        for key in INTEGRATION_REVIEW_CLAIM_KEYS:
            self.assertIn(key, section)

    def test_the_derived_fields_are_documented_as_not_the_reviewers_to_send(self):
        section = self.section()
        self.assertIn("run_id", section)
        self.assertIn("clearance", section)


class MailboxClosesTheRunTests(unittest.TestCase):
    """The real product path: request → mailbox → complete."""

    maxDiff = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v45-closeout-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)

    def cli(self, *argv):
        return run_cli(list(argv) + ["--json"], self.root)

    def serve(self, reviewer_evidence, crash_once_on_acceptance=False):
        """Publish, authorize and serve every mailbox request to a terminus."""
        self.assertEqual(self.cli("init", "--confirm").payload["status"], "ok")
        (self.root / "facts.json").write_text(json.dumps(FACTS), encoding="utf-8")
        self.cli("attest", "--adapter", "claude-code", "--facts", "facts.json",
                 "--provenance", "closeout e2e: native visible-task tools observed",
                 "--project-id", "closeoutprobe")
        shutil.copy(FIXTURE, self.root / "product-spec.json")
        self.cli("plan", "--request", REQUEST, "--plan-id", "closeout", "--from-prd", "product-spec.json")
        self.cli("authorize", "--plan", "closeout", "--authorize", "AUTHORIZE")
        result = self.cli("monitor", "--plan", "closeout", "--authorize", "AUTHORIZE")
        run_id = result.payload["run_id"]
        store = ProviderActionStore(self.paths)
        waits = {}
        crashed = []
        real_save = monitor.save_snapshot

        def crashing_save(paths, snapshot):
            """Lose exactly the save that would have persisted the acceptance."""
            if not crashed and snapshot.nodes.get("integration-review", {}).get("status") == "accepted":
                crashed.append(True)
                monitor.save_snapshot = real_save
                raise OSError("crash after the event landed, before the snapshot did")
            return real_save(paths, snapshot)

        if crash_once_on_acceptance:
            monitor.save_snapshot = crashing_save
            self.addCleanup(setattr, monitor, "save_snapshot", real_save)
        for _ in range(80):
            for action in store.pending():
                store.complete(action["action_id"], self.reply(action, waits, reviewer_evidence))
            try:
                result = self.cli("resume", "--plan", "closeout")
            except OSError:
                continue
            if result.payload.get("status") == "complete":
                break
        if crash_once_on_acceptance:
            self.assertTrue(crashed, "the crash hook never fired")
        return result, load_snapshot(self.paths, run_id)

    @staticmethod
    def reply(action, waits, reviewer_evidence):
        operation = action["operation"]
        if operation == "create":
            return {"binding": {"task_id": "sess_" + action["action_id"][:8], "host": "probe"}}
        if operation == "locate":
            return {"located": True}
        if operation == "visibility":
            return {"visible": True, "direct_enter": True}
        if operation == "resume":
            return {"resumed": True}
        key = (action.get("issue_id"), action.get("role"), action.get("generation"))
        waits[key] = waits.get(key, 0) + 1
        cursor = "c_%s_%d" % (action["action_id"][:6], waits[key])
        if waits[key] == 1:
            return {"status": "timeout", "cursor": cursor}
        if action.get("role") == "reviewer":
            return {"status": "completed", "cursor": cursor, "event": "accepted",
                    "evidence": reviewer_evidence}
        return {"status": "completed", "cursor": cursor, "event": "complete",
                "delivery_evidence": {"completion_marker": "done",
                                      "delivery_path": "src/export.ts",
                                      "thread_status": "complete"}}

    def test_a_structured_reviewer_acceptance_closes_the_run(self):
        result, snapshot = self.serve(CLEARED_CLAIM)
        self.assertEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.status, "complete")
        package = {key: value for key, value in snapshot.integration_review_evidence.items()
                   if key != "history"}
        validate_integration_review_evidence(snapshot, package)
        self.assertEqual(package["clearance"], {"p0": 0, "p1": 0, "p2": 0})
        self.assertEqual(package["run_id"], snapshot.run_id)
        self.assertEqual(package["aggregated_scope"]["nodes"],
                         [node for node in snapshot.nodes if node != "integration-review"])
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)
        self.assertIn("整合", result.text[0] if isinstance(result.text, tuple) else result.text)

    def test_a_free_form_reviewer_acceptance_is_visibly_blocked_not_silently_stranded(self):
        result, snapshot = self.serve("P0-P2 cleared")
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.nodes["integration-review"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.integration_review_evidence, {})

    def test_an_acceptance_that_still_reports_findings_is_blocked(self):
        claim = dict(CLEARED_CLAIM, findings=[
            {"severity": "p1", "status": "open", "detail": "事务边界未验证"},
        ])
        result, snapshot = self.serve(claim)
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.nodes["integration-review"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.integration_review_evidence, {})

    def test_a_waived_finding_does_not_close_the_run(self):
        claim = dict(CLEARED_CLAIM, findings=[
            {"severity": "p0", "status": "waived", "detail": "资金结算路径没验证，本轮先放行"},
        ])
        result, snapshot = self.serve(claim)
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.nodes["integration-review"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.integration_review_evidence, {})

    def test_a_verdict_the_disk_cannot_keep_never_reports_complete(self):
        """The CLI must not announce a closeout the next read rolls back.

        With a nested object under `evidence` the first serve reported
        `complete`, then `load_snapshot` rejected the persisted package and fell
        back to the previous snapshot -- so the acceptance was lost and the run
        reverted to `blocked_unknown` for good.
        """
        claim = dict(CLEARED_CLAIM,
                     iteration_compatibility={"status": "verified", "evidence": {"secret_scan": "clean"}})
        result, snapshot = self.serve(claim)
        self.assertNotEqual(result.payload["status"], "complete", result.payload)
        self.assertEqual(snapshot.nodes["integration-review"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.integration_review_evidence, {})

    def test_the_scope_the_reviewer_is_held_to_comes_from_the_plan(self):
        """The reviewer never sends these two lists, so the plan must supply them.

        Hardcoding either one in the monitor left every other assertion green,
        which is the whole claim of this change ("a reviewer cannot shrink the
        scope it is held to") going untested.
        """
        _, snapshot = self.serve(CLEARED_CLAIM)
        plan = json.loads((self.root / ".vibe" / "plans" / "closeout" / "plan.json").read_text(encoding="utf-8"))
        contract = plan["integration_contract"]
        package = snapshot.integration_review_evidence
        self.assertEqual(package["agentsmd_acceptance_refs"], contract["agentsmd_acceptance_refs"])
        self.assertEqual(package["unverified_or_excluded"], contract["unverified_or_excluded"])
        self.assertTrue(contract["unverified_or_excluded"], contract)

    def test_a_crash_before_the_acceptance_is_saved_recovers_by_re_reporting(self):
        """Recovery cannot rebuild the claim, so it asks for it again.

        The appended event keeps only redacted provider text, so replaying it
        could never reproduce the reviewer's judgement.  The replay door fails
        closed instead of inventing a package, which lets the monitor dispatch
        a second reviewer generation whose acceptance closes the run out for
        real.  A rebuilt-from-the-event package would leave the generation at 1.
        """
        result, snapshot = self.serve(CLEARED_CLAIM, crash_once_on_acceptance=True)
        integration = snapshot.nodes["integration-review"]
        self.assertGreater(integration["review_generation"], 1)
        self.assertEqual(result.payload["status"], "complete", result.payload)
        package = {key: value for key, value in snapshot.integration_review_evidence.items()
                   if key != "history"}
        validate_integration_review_evidence(snapshot, package)
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)


if __name__ == "__main__":
    unittest.main()
