import tempfile
import unittest
from pathlib import Path

from vibe_guide.evidence import (
    GenerationEvidence, replay_summary, write_generation_evidence,
)
from vibe_guide.paths import ProjectPaths


class V38EvidenceReplayTests(unittest.TestCase):
    def test_generation_files_are_immutable_and_summary_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            first = GenerationEvidence("run", "V38-1", 0, "task", "cursor", ".vibe/w", "codex/v38", "a" * 40, "accepted")
            second = GenerationEvidence("run", "V38-1", 1, "task", "cursor-2", ".vibe/w", "codex/v38", "a" * 40, "accepted")
            write_generation_evidence(paths, first)
            write_generation_evidence(paths, second)
            summary = replay_summary(paths, "run", "V38-1")
            self.assertEqual(summary.generations, [0, 1])
            self.assertEqual(summary.original_task_id, "task")


if __name__ == "__main__":
    unittest.main()
