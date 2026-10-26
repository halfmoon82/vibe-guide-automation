"""#275: developer rework prompts must carry this round's reviewer findings.

The continuation prompt (``vibe resume`` for visible tasks, the materialized
``provider-actions/prompts/<run>-<node>-developer-g<N>.md`` for background
ones) previously repeated only the business contract, so the reworked session
never saw the finding it was sent back to fix.  These tests pin:

* findings (severity/title/detail/fix and every other field, P0-P2) appear
  verbatim -- provider-text keys keep the existing key-name projection;
* a round with no findings leaves the prompt byte-identical;
* a missing reviewer delivery never produces fabricated content.
"""

import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.prompts import rework_findings_section
from vibe_guide.state import _REDACTED_PROVIDER_TEXT


RUN = "run-testfindings0000000000000000000"
NODE = "issue-275"


def _contract(root, **overrides):
    contract = {
        "node_id": NODE,
        "role": "developer",
        "phase": "rework",
        "generation": 4,
        "run_id": RUN,
        "project_root": str(root),
        "continuation": True,
        "output": "完成返工",
    }
    contract.update(overrides)
    return contract


def _write_delivery(root, generation, payload):
    deliveries = Path(root) / ".vibe" / "provider-actions" / "deliveries"
    deliveries.mkdir(parents=True, exist_ok=True)
    path = deliveries / "{}-{}-reviewer-g{}.json".format(RUN, NODE, generation)
    path.write_text(
        json.dumps(
            {
                "run_id": RUN,
                "issue_id": NODE,
                "role": "reviewer",
                "generation": generation,
                "payload": payload,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class ReworkFindingsSectionTests(unittest.TestCase):
    def test_findings_appear_verbatim_in_rework_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(
                root,
                2,
                {
                    "event": "review_finding",
                    "finding": {
                        "severity": "P1",
                        "title": "心跳没有预检",
                        "detail": "细节原文应被按键名规则打码",
                        "fix": "心跳派发前跑一次 resume-preflight",
                    },
                },
            )
            prompt = ProviderActionRunner._continuation_prompt(_contract(root))
        self.assertIn('"severity": "P1"', prompt)
        self.assertIn('"title": "心跳没有预检"', prompt)
        self.assertIn('"fix": "心跳派发前跑一次 resume-preflight"', prompt)
        # ``detail`` is a provider-text key: the existing key-name rules mask
        # its value rather than dropping the field silently.
        self.assertIn('"detail": "{}"'.format(_REDACTED_PROVIDER_TEXT), prompt)

    def test_all_p0_p2_findings_and_extra_findings_are_carried(self):
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(
                root,
                1,
                {
                    "event": "review_finding",
                    "finding": {"severity": "P0", "summary": "主 finding", "fix": "修主项"},
                    "extra_findings": [
                        {"severity": "P2", "summary": "附带 finding"},
                        {"severity": "P3", "summary": "观测项不进返工提示"},
                    ],
                    "evidence": {"findings": [{"severity": "P1", "title": "证据内 finding"}]},
                },
            )
            section = rework_findings_section(_contract(root))
        self.assertIn("主 finding", section)
        self.assertIn("附带 finding", section)
        self.assertIn("证据内 finding", section)
        self.assertNotIn("观测项不进返工提示", section)

    def test_latest_review_finding_generation_wins(self):
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(root, 1, {"event": "review_finding", "finding": {"severity": "P1", "fix": "旧轮次"}})
            _write_delivery(root, 2, {"event": "accepted", "evidence": {}})
            _write_delivery(root, 3, {"event": "review_finding", "finding": {"severity": "P2", "fix": "本轮修复点"}})
            section = rework_findings_section(_contract(root))
        self.assertIn("本轮修复点", section)
        self.assertNotIn("旧轮次", section)

    def test_no_findings_writes_no_placeholder(self):
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(root, 1, {"event": "accepted", "evidence": {"conclusion": "P0-P2 已清零"}})
            prompt = ProviderActionRunner._continuation_prompt(_contract(root))
        self.assertNotIn("reviewer findings", prompt)

    def test_missing_delivery_leaves_prompt_unchanged(self):
        contract = {"node_id": NODE, "role": "developer", "phase": "rework",
                    "generation": 2, "run_id": RUN, "output": "完成返工"}
        baseline = ProviderActionRunner._continuation_prompt(dict(contract))
        with tempfile.TemporaryDirectory() as root:
            with_findings_path = ProviderActionRunner._continuation_prompt(
                _contract(root, **{k: v for k, v in contract.items() if k not in ("run_id",)})
            )
        self.assertEqual(baseline, with_findings_path)

    def test_non_rework_phases_and_reviewer_role_skip_the_section(self):
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(
                root, 1, {"event": "review_finding", "finding": {"severity": "P0", "fix": "x"}}
            )
            for override in (
                {"phase": "develop"},
                {"role": "reviewer"},
            ):
                contract = _contract(root, **override)
                self.assertEqual("", rework_findings_section(contract))
                self.assertNotIn(
                    "reviewer findings",
                    ProviderActionRunner._continuation_prompt(contract),
                )

    def test_string_finding_is_carried_with_round_severity(self):
        # Monitor._review_finding_disposition sends {"event": "review_finding",
        # "finding": "fix test"} back as an implementation finding; the rework
        # prompt must not drop it just because the carrier is a string.
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(
                root,
                1,
                {
                    "event": "review_finding",
                    "severity": "P1",
                    "finding": "fix test",
                },
            )
            section = rework_findings_section(_contract(root))
        self.assertIn("fix test", section)
        self.assertIn('"severity": "P1"', section)
        # The synthesized entry must not reuse the ``finding`` key: it is a
        # provider-text key and the durable projection would mask the text.
        self.assertIn('"summary": "fix test"', section)

    def test_top_level_review_finding_and_nested_carriers_are_recognized(self):
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(
                root,
                1,
                {
                    "event": "review_finding",
                    "review_finding": {
                        "severity": "P1",
                        "title": "顶层载体",
                        "fix": "对齐 monitor 口径",
                    },
                },
            )
            _write_delivery(
                root,
                2,
                {
                    "event": "review_finding",
                    "finding": {
                        "severity": "P2",
                        "finding": {
                            "severity": "P2",
                            "title": "嵌套 finding.finding",
                        },
                    },
                },
            )
            section = rework_findings_section(_contract(root))
        self.assertIn("嵌套 finding.finding", section)
        self.assertNotIn("顶层载体", section)  # newer generation wins

    def test_p4_p5_findings_ride_along_like_monitor_disposition(self):
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(
                root,
                1,
                {
                    "event": "review_finding",
                    "finding": {"severity": "P4", "summary": "低危返工项"},
                },
            )
            section = rework_findings_section(_contract(root))
        self.assertIn("低危返工项", section)

    def test_duplicate_finding_across_carriers_is_deduped(self):
        shared = {"severity": "P1", "title": "同一条 finding"}
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(
                root,
                1,
                {
                    "event": "review_finding",
                    "finding": dict(shared),
                    "evidence": {"findings": [dict(shared)]},
                },
            )
            section = rework_findings_section(_contract(root))
        self.assertEqual(1, section.count("同一条 finding"))

    def test_sensitive_keys_are_dropped_from_findings(self):
        with tempfile.TemporaryDirectory() as root:
            _write_delivery(
                root,
                1,
                {
                    "event": "review_finding",
                    "finding": {
                        "severity": "P1",
                        "fix": "去掉硬编码凭据",
                        "token": "secret-value-must-not-leak",
                    },
                },
            )
            section = rework_findings_section(_contract(root))
        self.assertNotIn("secret-value-must-not-leak", section)
        self.assertIn('"token": "[REDACTED]"', section)


if __name__ == "__main__":
    unittest.main()
