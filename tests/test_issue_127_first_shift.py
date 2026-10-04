"""ISSUE-127: a first supervisor shift must be told to register and to beat.

5.0.1 shipped the address registry, the preflight and the rotate decision, but
`supervisor-register` and the heartbeat were only ever named inside the rotate
branch of §6.5.  A first shift therefore never announced itself: a worker's
completion wake-up had no address to target (`supervisor-address` answered
`unknown`), and nothing called the preflight periodically, so the
token-threshold rotation never fired.

These tests pin the three places the fix lands -- the shipped prd-guide
protocol, the entry protocol, and `vibe monitor`'s first start -- so the duty
cannot quietly fall back out of the start-of-shift flow.
"""
import unittest

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import load_protocol
from vibe_guide.supervisor import (
    current_supervisor_address,
    register_supervisor_address,
)

from tests.support_v45_authorize import publish_complex_probe


class PrdGuideOpeningStepTests(unittest.TestCase):
    """§6 must open with the two duties, before it serves the mailbox."""

    def section_and_opening(self):
        text = load_protocol("prd-guide")
        start = text.index("## 6. 服务监工信箱")
        section = text[start:text.index("\n## 7. ", start)]
        opening_start = section.index("### 6.0")
        opening_end = section.find("\n### ", opening_start)
        opening = (
            section[opening_start:]
            if opening_end == -1
            else section[opening_start:opening_end]
        )
        return section, opening

    def test_opening_step_precedes_the_mailbox_round(self):
        # Ordered, not merely present: §6.5 names both commands too, so a
        # document-wide `assertIn` stays green even with this step missing.
        section, _opening = self.section_and_opening()
        self.assertLess(section.index("### 6.0"), section.index("### 6.1"))

    def test_opening_step_names_the_registration_command(self):
        _section, opening = self.section_and_opening()
        self.assertIn("vibe supervisor-register", opening)
        self.assertIn("supervisor-address", opening)

    def test_opening_step_makes_the_preflight_the_first_beat(self):
        _section, opening = self.section_and_opening()
        self.assertIn("vibe supervisor-preflight", opening)
        self.assertIn("心跳", opening)

    def test_opening_step_states_what_breaks_without_it(self):
        _section, opening = self.section_and_opening()
        self.assertIn("唤醒", opening)
        self.assertIn("rotate", opening)


class EntryProtocolFirstShiftTests(unittest.TestCase):
    def complex_path(self):
        text = load_protocol("vibe-entry")
        return text[text.index("## 3."):text.index("## 4.")]

    def test_first_shift_is_a_fixed_step_of_the_complex_path(self):
        path = self.complex_path()
        self.assertIn("supervisor-register", path)
        self.assertIn("supervisor-preflight", path)

    def test_first_shift_points_at_the_opening_section(self):
        self.assertIn("6.0", self.complex_path())


class MonitorFirstShiftTests(unittest.TestCase):
    def _start(self, as_json=False):
        root = publish_complex_probe(self)
        run_cli(
            ["authorize", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"],
            root,
        )
        args = ["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE"]
        if as_json:
            args.insert(1, "--json")
        return root, run_cli(args, root)

    def test_first_start_prints_register_and_heartbeat(self):
        _root, started = self._start()
        self.assertIsNotNone(started.payload.get("run_id"), started.text)
        self.assertIn("vibe supervisor-register", started.text)
        self.assertIn("vibe supervisor-preflight", started.text)

    def test_a_registered_shift_is_not_told_again(self):
        root, started = self._start(as_json=True)
        run_id = started.payload["run_id"]
        paths = ProjectPaths(root)
        self.assertEqual(
            current_supervisor_address(paths, run_id).get("status"), "unknown"
        )
        register_supervisor_address(
            paths, run_id,
            {"provider": "codex", "session_id": "s-1", "host": "mac"},
        )
        again = run_cli(
            ["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE"], root
        )
        self.assertIsNotNone(again.payload.get("run_id"), again.text)
        self.assertNotIn("vibe supervisor-register", again.text)


if __name__ == "__main__":
    unittest.main()
