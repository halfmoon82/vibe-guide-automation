"""Issue #280: `vibe status --json` keeps `nodes` a stable top-level object.

run-64c826b5 showed the chat-side byte budget replacing the whole JSON with a
bare ``output_evidence`` reference, so the old reader ``payload['nodes']``
silently broke.  Truncation may still happen, but only as an explicit
``truncated`` flag plus a pointer to the full output on disk -- the top-level
shape must not change.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import CLIResult, main
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import SUPERVISOR_OUTPUT_LIMIT, bounded_supervisor_output


def _big_nodes(count):
    return {
        "node-{:03d}".format(index): {
            "status": "running",
            "user_status": "已启动",
            "evidence": ["x" * 2000],
        }
        for index in range(count)
    }


class StatusJsonShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".vibe").mkdir()
        (self.root / "AGENTS.md").write_text("test project marker")
        self.paths = ProjectPaths(self.root)

    def _run_status_main(self, payload):
        result = CLIResult(0, payload, "text", True)
        output = io.StringIO()
        with patch("vibe_guide.cli.run_cli", return_value=result), \
                patch("vibe_guide.cli.Path.cwd", return_value=self.root), \
                redirect_stdout(output):
            code = main(["status", "--run-id", "run-1", "--json"])
        return code, json.loads(output.getvalue())

    def test_oversized_status_json_keeps_nodes_object(self):
        payload = {
            "command": "status",
            "status": "running",
            "run_id": "run-1",
            "nodes": _big_nodes(40),
        }
        # Sanity: the rendered payload must actually exceed the budget.
        self.assertGreater(
            len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            SUPERVISOR_OUTPUT_LIMIT,
        )
        code, short = self._run_status_main(payload)
        self.assertEqual(code, 0)
        self.assertIsInstance(short.get("nodes"), dict, short)
        self.assertEqual(set(short["nodes"]), set(payload["nodes"]))
        self.assertTrue(short.get("truncated"))
        ref = short["output_evidence"]
        self.assertEqual(
            json.loads(Path(ref["path"]).read_text())["nodes"].keys(),
            payload["nodes"].keys(),
        )

    def test_repeated_status_json_top_level_shape_is_stable(self):
        """Acceptance: two consecutive reads share the same top-level shape."""
        payload = {
            "command": "status",
            "status": "running",
            "run_id": "run-1",
            "nodes": _big_nodes(40),
        }
        _code, first = self._run_status_main(payload)
        _code, second = self._run_status_main(payload)
        self.assertEqual(set(first), set(second))
        self.assertIsInstance(first["nodes"], dict)
        self.assertIsInstance(second["nodes"], dict)

    def test_bounded_output_without_nodes_is_unchanged(self):
        payload = {"command": "resume", "run_id": "run-1",
                   "status": "blocked_unknown", "reason": "y" * 20000}
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        short = json.loads(
            bounded_supervisor_output(self.paths, "run-1", payload, rendered)
        )
        self.assertNotIn("nodes", short)
        self.assertTrue(short["truncated"])
        self.assertIn("output_evidence", short)

    def test_non_dict_nodes_value_is_not_rewritten(self):
        # ``nodes`` was a list in one historical payload shape; the guard only
        # touches dicts and never invents a structure for other types.
        payload = {"command": "status", "run_id": "run-1", "status": "x",
                   "nodes": ["a" * 20000]}
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        short = json.loads(
            bounded_supervisor_output(self.paths, "run-1", payload, rendered)
        )
        self.assertTrue(short["truncated"])
        self.assertNotIn("nodes", short)

    def test_node_projection_falls_back_to_empty_objects(self):
        # Even absurdly many node ids must keep ``nodes`` an object; per-node
        # detail may degrade to {} but the key set stays complete.
        payload = {
            "command": "status",
            "run_id": "run-1",
            "status": "running",
            "nodes": {"node-id-{:04d}".format(i):
                      {"status": "running", "user_status": "已启动" * 40}
                      for i in range(250)},
        }
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        short = json.loads(
            bounded_supervisor_output(self.paths, "run-1", payload, rendered)
        )
        self.assertIsInstance(short.get("nodes"), dict)
        self.assertEqual(set(short["nodes"]), set(payload["nodes"]))
        self.assertTrue(all(not node for node in short["nodes"].values()))
        self.assertLessEqual(len(json.dumps(short).encode("utf-8")),
                             SUPERVISOR_OUTPUT_LIMIT)


if __name__ == "__main__":
    unittest.main()
