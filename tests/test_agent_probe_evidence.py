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


if __name__ == "__main__":
    unittest.main()
