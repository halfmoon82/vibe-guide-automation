"""The `<adapter>.agent` fact a session attests must reach the detection evidence.

`detect()` evaluates the manifest's `command`-kind agent probe through
`Environment.has_command`; the bridge reader used to hand facts only to
`Environment.facts`, so the recorded evidence for `<adapter>.agent` was always
False regardless of what the session stated.  The doctor payload and any
consumer of `DetectionResult.evidence` therefore misreported it.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.node_spec import observe_capabilities
from vibe_guide.paths import ProjectPaths

FACTS = {name: True for name in (
    "claude-code.agent", "claude-code.shell", "claude-code.subprocess", "claude-code.worktree",
    "claude-code.visible_task.create", "claude-code.visible_task.enter",
    "claude-code.visible_task.resume", "claude-code.visible_task.wait",
)}


class AgentProbeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vg-agent-probe-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)
        self.assertEqual(run_cli(["init", "--confirm", "--json"], self.root).payload["status"], "ok")

    def write(self, facts):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(json.dumps({
            "schema_version": 1, "adapter_id": "claude-code", "facts": facts, "provenance": "test",
        }), encoding="utf-8")

    def test_attested_agent_fact_is_reflected_in_detection_evidence(self):
        self.write(FACTS)
        observed = observe_capabilities(self.paths)
        self.assertTrue(observed.detection.evidence["claude-code.agent"])
        self.write({**FACTS, "claude-code.agent": False})
        observed = observe_capabilities(self.paths)
        self.assertFalse(observed.detection.evidence["claude-code.agent"])

    def test_only_the_agent_evidence_changes_with_the_agent_fact(self):
        self.write(FACTS)
        with_agent = observe_capabilities(self.paths)
        self.write({**FACTS, "claude-code.agent": False})
        without_agent = observe_capabilities(self.paths)
        strip = lambda evidence: {k: v for k, v in evidence.items() if k != "claude-code.agent"}
        self.assertEqual(strip(with_agent.detection.evidence), strip(without_agent.detection.evidence))
        self.assertEqual(with_agent.capabilities, without_agent.capabilities)
        self.assertEqual(with_agent.detection.detected, without_agent.detection.detected)
        self.assertEqual(with_agent.capabilities["level"], "full")
        self.write({**FACTS, "claude-code.subprocess": False})
        self.assertEqual(observe_capabilities(self.paths).capabilities["level"], "guide")

    def test_attested_in_session_sdd_fact_reaches_evidence_provenance_and_topology(self):
        self.write({**FACTS, "claude-code.in_session_sdd": True})
        observed = observe_capabilities(self.paths)
        self.assertTrue(observed.detection.evidence["claude-code.in_session_sdd"])
        self.assertEqual(
            observed.detection.capabilities.provenance["claude-code.in_session_sdd"], "test"
        )
        from vibe_guide.adapters.base import Environment
        from vibe_guide.adapters.registry import AdapterRegistry
        environment = Environment(
            facts={**FACTS, "claude-code.in_session_sdd": True},
            provenance={name: "test" for name in {**FACTS, "claude-code.in_session_sdd": True}},
        )
        adapter = AdapterRegistry().get("claude-code")
        decision = adapter.topology_decision(environment)
        self.assertEqual((decision.topology, decision.probe_status), ("in_session_sdd", "pass"))
        self.assertEqual(decision.evidence_ref, "test")
        report = adapter.capability_report(environment)
        self.assertEqual(report["topology"]["topology"], "in_session_sdd")
        self.assertEqual(report["topology"]["evidence_ref"], "test")

    def test_missing_in_session_sdd_fact_stays_unknown_and_conservative(self):
        self.write(FACTS)
        observed = observe_capabilities(self.paths)
        self.assertFalse(observed.detection.evidence["claude-code.in_session_sdd"])
        from vibe_guide.adapters.base import Environment
        from vibe_guide.adapters.registry import AdapterRegistry
        adapter = AdapterRegistry().get("claude-code")
        decision = adapter.topology_decision(Environment(facts=FACTS))
        self.assertEqual((decision.topology, decision.probe_status), ("dual-visible", "unknown"))
        report = adapter.capability_report(Environment(facts=FACTS))
        self.assertEqual(report["topology"]["probe_status"], "unknown")
        self.assertIsNone(report["topology"]["evidence_ref"])


if __name__ == "__main__":
    unittest.main()
