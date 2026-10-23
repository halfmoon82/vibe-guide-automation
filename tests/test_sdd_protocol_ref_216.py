"""#216 ②: visible-sdd protocol delivery switch (inline default, opt-in ref)."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import (
    SHIPPED_PROTOCOL_DIGESTS,
    load_protocol,
    protocol_path,
)
from tests.test_issue_93_protocol_delivery import CONTRACT, _RecordingRunner

NAME = "visible-sdd-worker"
INLINE_HEADER = "## 会话必须遵循的协议（随包全文，逐字执行）\n\n"


def _prompt(root, config=None):
    if config is not None:
        (Path(root) / ".vibe").mkdir(exist_ok=True)
        (Path(root) / ".vibe" / "config.json").write_text(json.dumps(config))
    runner = _RecordingRunner(ProjectPaths(Path(root)))
    runner.task_binding(dict(CONTRACT), Path(root), "run-216", "running")
    creates = [r for op, r in runner.requests if op == "create"]
    return runner, creates[0]["prompt"] if creates else None


class SddProtocolRefTests(unittest.TestCase):
    def test_default_and_explicit_inline_are_identical_and_full_text(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _, default = _prompt(a)
            default = default.replace(a, "ROOT")
            _, inline = _prompt(b, {"sdd_protocol_delivery": "inline"})
            inline = inline.replace(b, "ROOT")
        self.assertEqual(default, inline)
        self.assertIn(INLINE_HEADER + load_protocol(NAME) + "\n\n", default)

    def test_ref_prompt_has_path_and_published_sha_without_full_text(self):
        with tempfile.TemporaryDirectory() as d:
            _, prompt = _prompt(d, {"sdd_protocol_delivery": "ref"})
        text = load_protocol(NAME)
        digest = hashlib.sha256(protocol_path(NAME).read_bytes()).hexdigest()
        self.assertNotIn(text, prompt)
        self.assertNotIn("## 1. 固定流程", prompt)
        self.assertIn(str(protocol_path(NAME)), prompt)
        self.assertIn(digest, prompt)
        self.assertIn(digest, SHIPPED_PROTOCOL_DIGESTS[NAME])
        self.assertIn("摘要不符", prompt)
        self.assertLess(len(prompt), len(text) // 2)
        # consistency block stays last and parseable
        json.loads(prompt.split("一致性纠偏证据必须原样绑定：", 1)[1])

    def test_ref_fails_closed_when_protocol_digest_unpublished(self):
        with tempfile.TemporaryDirectory() as d:
            real = protocol_path(NAME).read_bytes()
            with patch(
                "vibe_guide.protocols.protocol_path",
                return_value=Path(d) / "edited.md",
            ):
                (Path(d) / "edited.md").write_bytes(real + b"\nedited\n")
                with self.assertRaises(ValueError):
                    _prompt(d, {"sdd_protocol_delivery": "ref"})

    def test_ref_does_not_leak_create_when_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(SHIPPED_PROTOCOL_DIGESTS, {NAME: frozenset({"0" * 64})}):
                (Path(d) / ".vibe").mkdir()
                (Path(d) / ".vibe" / "config.json").write_text(
                    json.dumps({"sdd_protocol_delivery": "ref"}))
                runner = _RecordingRunner(ProjectPaths(Path(d)))
                with self.assertRaises(ValueError):
                    runner.task_binding(dict(CONTRACT), Path(d), "run-216", "running")
                self.assertEqual(runner.requests, [])

    def test_illegal_delivery_value_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".vibe").mkdir()
            (Path(d) / ".vibe" / "config.json").write_text(
                json.dumps({"sdd_protocol_delivery": "Ref"}))
            runner = _RecordingRunner(ProjectPaths(Path(d)))
            with self.assertRaises(ValueError):
                runner.task_binding(dict(CONTRACT), Path(d), "run-216", "running")
            self.assertEqual(runner.requests, [])


if __name__ == "__main__":
    unittest.main()
