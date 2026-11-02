"""#381: rework and dispatch prompts state the auto-scope rule in one sentence.

Recorded failure (run-3218a7e6, v5.2.4): the delivery gate auto-accepts
``tests/`` files and project-registered mechanical files, but the prompts did
not say so; three developers each parked an out-of-scope ``tests/`` assertion
or the protocol digest registry (``vibe_guide/protocols/__init__.py``) for the
supervisor, costing one extra round per node.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide import scope_landing
from vibe_guide.authorization import WORKER_PUSH_BAN_TEXT
from vibe_guide.paths import ProjectPaths
from vibe_guide.prompts import AUTO_SCOPE_RULE_PREFIX, auto_scope_rule_sentence
from vibe_guide.runners.provider_action import ProviderActionRunner

from tests import test_scope_landing as _landing

PROTOCOL_REGISTRY = "vibe_guide/protocols/__init__.py"


def _write_config(root: Path, data: dict) -> None:
    (root / ".vibe").mkdir(parents=True, exist_ok=True)
    (root / ".vibe" / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _contract(role="developer", generation=2):
    return {"node_id": "n1", "role": role, "generation": generation, "run_id": "run-1",
            "files": ["vibe_guide/prd.py"], "worktree": ".worktrees/n1", "branch": "node/n1"}


class SentenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_sentence_names_tests_and_the_registered_paths(self):
        _write_config(self.root, {"auto_scope_paths": [PROTOCOL_REGISTRY]})
        sentence = auto_scope_rule_sentence(self.root)
        self.assertTrue(sentence.startswith(AUTO_SCOPE_RULE_PREFIX))
        self.assertIn("tests/", sentence)
        self.assertIn(PROTOCOL_REGISTRY, sentence)
        self.assertIn("不需要监工中转", sentence)
        # One sentence: a single full stop, at the end.
        self.assertEqual(sentence.count("。"), 1)
        self.assertTrue(sentence.endswith("。"))
        self.assertNotIn("验收例子判定结果", sentence)

    def test_approved_acceptance_rule_is_mentioned_only_when_approved(self):
        _write_config(self.root, {"acceptance_scope_rule_approved": True})
        self.assertIn("验收例子判定结果", auto_scope_rule_sentence(self.root))

    def test_invalid_config_never_widens_the_sentence(self):
        _write_config(self.root, {"auto_scope_paths": "not-a-list",
                                  "acceptance_scope_rule_approved": True})
        sentence = auto_scope_rule_sentence(self.root)
        self.assertIn("tests/", sentence)
        self.assertNotIn("验收例子判定结果", sentence)
        self.assertNotIn("not-a-list", sentence)


class ReworkPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _write_config(self.root, {"auto_scope_paths": [PROTOCOL_REGISTRY]})

    def test_rework_prompt_states_the_rule(self):
        prompt = ProviderActionRunner._continuation_prompt(_contract(), self.root)
        self.assertIn(auto_scope_rule_sentence(self.root), prompt)
        self.assertIn(PROTOCOL_REGISTRY, prompt)
        # The old wording told the developer to leave every out-of-scope file
        # to the supervisor, contradicting the gate.
        self.assertNotIn("需要改允许路径外的文件时，不要改路径外文件", prompt)
        self.assertIn("不在自动放行范围内", prompt)

    def test_reviewer_prompt_does_not_invite_edits(self):
        prompt = ProviderActionRunner._continuation_prompt(_contract("reviewer"), self.root)
        self.assertNotIn(AUTO_SCOPE_RULE_PREFIX, prompt)

    def test_first_dispatch_prompt_states_the_rule_and_the_push_ban(self):
        runner = ProviderActionRunner(ProjectPaths(self.root), "codex", "codex-app-visible")
        seen = []

        def fake(c, r, operation, request):
            seen.append((operation, request))
            return {"create": {"binding": {"task_id": "t", "host": "mac"}},
                    "locate": {"located": True},
                    "visibility": {"visible": True, "direct_enter": True}}[operation]

        runner._require_result = fake
        runner.task_binding({
            "run_id": "run-1", "node_id": "g2", "role": "developer", "generation": 1,
            "project_id": "p", "topology": "dual-visible", "worktree": "w", "branch": "b",
            "files": [], "remote_git_actions": "allow",
        }, Path(tempfile.mkdtemp()), "run-1", "running")
        prompt = dict(seen)["create"]["prompt"]
        self.assertIn(auto_scope_rule_sentence(self.root), prompt)
        self.assertIn(WORKER_PUSH_BAN_TEXT, prompt)


class RegisteredRegistryLandsTests(_landing.LandingTests):
    """A rework developer edits the protocol digest registry: accepted directly."""

    def test_registered_protocol_registry_is_auto_expanded(self):
        _write_config(self.paths.root, {"auto_scope_paths": [PROTOCOL_REGISTRY]})
        self.land("bg", {"bg.py": "work\n", PROTOCOL_REGISTRY: "DIGESTS = {}\n"})
        snapshot = self.deliver()
        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "review", node.get("reason"))
        self.assertEqual(node["scope_expansions"], [PROTOCOL_REGISTRY])
        [expanded] = self.events("scope_auto_expanded")
        [rule] = expanded["data"]["scope_rules"]
        self.assertEqual(rule["rule"], "auto_scope_paths")

    def test_unregistered_protocol_registry_still_needs_the_supervisor(self):
        self.land("bg", {"bg.py": "work\n", PROTOCOL_REGISTRY: "DIGESTS = {}\n"})
        snapshot = self.deliver()
        self.assertEqual(snapshot.nodes["bg"]["scope_landing_rejection"],
                         [{"path": PROTOCOL_REGISTRY, "cause": scope_landing.CAUSE_NOT_EXPANDABLE}])


# Only the two subclass tests above run from this module.
for _name in [n for n in dir(_landing.LandingTests) if n.startswith("test_")]:
    if _name not in RegisteredRegistryLandsTests.__dict__:
        setattr(RegisteredRegistryLandsTests, _name, None)


if __name__ == "__main__":
    unittest.main()
