"""Regression tests: a complex plan with zero product decisions must authorize.

`vibe plan --from-prd` legitimately publishes a complex plan whose
``decisions`` list is empty when the PRD surfaces no unresolved product
trade-offs (``approve_prd`` iterates an empty list without blockers).  The
authorize entry originally rejected that shape with ``blocked_design`` even
though every published artifact was valid.  These tests pin that an empty
decision list authorizes, while every tampered or malformed decision shape
still fails closed.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorize_entry import (
    AuthorizationDenied,
    build_workflow_evidence,
    materialize_workflow_evidence,
)
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.workflow_gate import verify_workflow

from tests.support_v45_authorize import _CAPABILITIES, _NODE_SPEC, _REQUEST


def _publish_probe(case, spec_overrides=None):
    """Publish a fresh complex plan, optionally overriding node-spec fields."""
    root = Path(tempfile.mkdtemp(prefix="empty-decisions-"))
    case.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))

    init = run_cli(["init", "--confirm", "--json"], root)
    assert init.payload.get("status") == "ok", init.payload

    store = root / ".vibe" / "provider-actions"
    store.mkdir(parents=True, exist_ok=True)
    (store / "capabilities.json").write_text(
        json.dumps(_CAPABILITIES, ensure_ascii=False), encoding="utf-8"
    )

    spec = json.loads(json.dumps(_NODE_SPEC))
    for key, value in (spec_overrides or {}).items():
        spec[key] = value
    spec_path = root / "node-spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    published = run_cli(
        [
            "plan", "--json", "--request", _REQUEST, "--s1", "5,5,5,5,5",
            "--plan-id", "probe-plan", "--node-spec", "node-spec.json",
        ],
        root,
    )
    assert published.payload.get("status") == "ok", published.payload
    return root


def _rewrite_plan_json(root, mutate):
    """Apply ``mutate`` to the published plan.json and atomically rewrite it."""
    plan_path = root / ".vibe" / "plans" / "probe-plan" / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    mutate(plan)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")


class EmptyDecisionsAuthorize(unittest.TestCase):
    def test_plan_with_zero_decisions_publishes_as_complex(self):
        root = _publish_probe(self, {"decisions": []})
        plan = json.loads(
            (root / ".vibe" / "plans" / "probe-plan" / "plan.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(plan.get("complexity_band"), "complex")
        self.assertEqual(plan.get("decisions"), [])
        self.assertEqual(plan.get("status"), "confirmed_pending_authorization")

    def test_authorize_accepts_a_published_empty_decision_list(self):
        root = _publish_probe(self, {"decisions": []})
        paths = ProjectPaths(root)
        workflow = build_workflow_evidence(paths, "probe-plan", "AUTHORIZE")
        self.assertEqual(verify_workflow(workflow), {
            "status": "complete",
            "authorization_granted": True,
        })
        record = workflow["node_records"]["product_decision"]
        self.assertEqual(record["input"]["decision_count"], 0)
        self.assertTrue(record["output"]["all_approved"])

    def test_authorize_cli_reports_ok_for_empty_decisions(self):
        root = _publish_probe(self, {"decisions": []})
        result = run_cli(
            ["authorize", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"],
            root,
        )
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload.get("status"), "ok")
        self.assertTrue(result.payload.get("authorization_granted"))

    def test_materialize_persists_ten_node_evidence_for_empty_decisions(self):
        root = _publish_probe(self, {"decisions": []})
        paths = ProjectPaths(root)
        workflow = materialize_workflow_evidence(paths, "probe-plan", "AUTHORIZE")
        self.assertEqual(len(workflow["node_records"]), 10)
        state = json.loads((root / ".vibe" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("task_workflow", {}).get("probe-plan", {}).get("route"), "complex")

    def test_wrong_token_is_still_denied_for_empty_decisions(self):
        root = _publish_probe(self, {"decisions": []})
        paths = ProjectPaths(root)
        for token in ("", "authorize", "AUTHORIZE_LATER", "yes"):
            with self.subTest(token=token):
                with self.assertRaises(AuthorizationDenied):
                    build_workflow_evidence(paths, "probe-plan", token)

    def test_tampered_non_approved_decision_still_fails_closed(self):
        """A decision edited after publication to non-approved must not pass.

        ``approve_prd`` blocks unresolved decisions at publish time, so this
        scenario is only reachable through artifact tampering; the authorize
        entry must still reject it as defense-in-depth.
        """
        root = _publish_probe(self, {"decisions": []})
        _rewrite_plan_json(
            root,
            lambda plan: plan["decisions"].append(
                {
                    "question": "事后注入",
                    "options": ["a", "b"],
                    "impact": "x",
                    "recommendation": "a",
                    "status": "pending",
                    "selected": None,
                    "field": "scope",
                }
            ),
        )
        with self.assertRaises(ValueError):
            build_workflow_evidence(ProjectPaths(root), "probe-plan", "AUTHORIZE")

    def test_tampered_approved_decision_fails_on_digest_mismatch(self):
        """Even a well-formed approved decision added later breaks the card digest."""
        root = _publish_probe(self, {"decisions": []})
        _rewrite_plan_json(
            root,
            lambda plan: plan["decisions"].append(
                {
                    "question": "事后批准项",
                    "options": ["a", "b"],
                    "impact": "x",
                    "recommendation": "a",
                    "status": "approved",
                    "selected": "a",
                    "field": "scope",
                }
            ),
        )
        with self.assertRaises(ValueError):
            build_workflow_evidence(ProjectPaths(root), "probe-plan", "AUTHORIZE")

    def test_non_list_decisions_fail_closed(self):
        """Falsy and truthy non-list decision values must both be rejected.

        Under the old ``plan.get("decisions") or []`` coercion, falsy
        non-lists were silently reshaped into ``[]``; these cases now pin
        that the type check -- not truthiness -- is what rejects them.
        """
        for bad in (0, "", {}, "yes", [0]):
            with self.subTest(decisions=bad):
                root = _publish_probe(self, {"decisions": []})
                _rewrite_plan_json(root, lambda plan, value=bad: plan.__setitem__("decisions", value))
                with self.assertRaises(ValueError):
                    build_workflow_evidence(
                        ProjectPaths(root), "probe-plan", "AUTHORIZE"
                    )

    def test_absent_decisions_key_normalizes_to_empty(self):
        """``decisions`` missing from plan.json authorizes like ``[]``.

        Publishing always writes the key, so absence is only reachable
        through post-publication editing; the recorded digest treats it
        identically to an empty list, which is the documented contract.
        """
        root = _publish_probe(self, {"decisions": []})
        _rewrite_plan_json(root, lambda plan: plan.pop("decisions"))
        workflow = build_workflow_evidence(ProjectPaths(root), "probe-plan", "AUTHORIZE")
        self.assertEqual(workflow["node_records"]["product_decision"]["input"]["decision_count"], 0)

    def test_falsy_non_list_decisions_are_stale_after_run_starts(self):
        """The lifecycle invariant applies the same type contract as authorize.

        ``plan.json`` is legitimately rewritten by dispatch, so after a run
        starts it is checked by content invariant rather than byte digest.
        Swapping ``decisions: []`` for ``false`` must be detected as stale
        evidence instead of being coerced back to an empty list.
        """
        from vibe_guide.authorize_entry import load_live_workflow, verify_workflow_artifacts

        root = _publish_probe(self, {"decisions": []})
        paths = ProjectPaths(root)
        granted = run_cli(
            ["authorize", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"],
            root,
        )
        self.assertEqual(granted.exit_code, 0, granted.payload)
        started = run_cli(
            ["monitor", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"],
            root,
        )
        self.assertIsNotNone(started.payload.get("run_id"), started.payload)
        _rewrite_plan_json(root, lambda plan: plan.__setitem__("decisions", False))
        workflow = load_live_workflow(paths, "probe-plan")
        with self.assertRaises(AuthorizationDenied) as caught:
            verify_workflow_artifacts(paths, workflow, run_started=True)
        self.assertIn("plan.json", str(caught.exception))

    def test_non_empty_approved_decisions_still_authorize(self):
        """The regression fix must not weaken the existing approved path."""
        root = _publish_probe(self)
        paths = ProjectPaths(root)
        workflow = build_workflow_evidence(paths, "probe-plan", "AUTHORIZE")
        self.assertEqual(verify_workflow(workflow), {
            "status": "complete",
            "authorization_granted": True,
        })
        record = workflow["node_records"]["product_decision"]
        self.assertEqual(record["input"]["decision_count"], 1)


if __name__ == "__main__":
    unittest.main()
