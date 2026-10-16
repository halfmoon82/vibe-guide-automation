"""The eval dataset must come from real decisions, not from documentation.

`eval/decision/build_from_history.py` mines routing decisions (`S1：…→…`) out of
Claude Code transcripts.  Two failure modes decide whether the resulting eval
means anything, and both are pinned here with synthetic transcripts:

1. **Provenance** — only the assistant's own prose counts.  Reading
   `vibe-entry.md` puts the protocol's example lines into the transcript as a
   tool result; scanning those would mint labels out of documentation, so a
   tool result carrying a perfect-looking decision line must yield nothing.
2. **Pairing** — a decision belongs to the immediately preceding user-role
   message.  Heartbeats and host injections are themselves scored turns; v1's
   "nearest accepted request" anchor attached their decisions to older user
   requests (11 phantom conflicts, 9 mislabeled rows).  A heartbeat between a
   request and a decision must therefore drop the decision, not re-attribute it.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BUILDER = _REPO_ROOT / "eval" / "decision" / "build_from_history.py"

_spec = importlib.util.spec_from_file_location("build_from_history", _BUILDER)
build_from_history = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_from_history)


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _assistant(*texts):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": t} for t in texts]},
    }


def _tool_result(text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}


def _write_transcript(root, entries, name="proj/session.jsonl"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return str(root / "proj" / "*.jsonl")


class BuildFromHistoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _build(self, entries):
        source = _write_transcript(self.root, entries)
        return build_from_history.build(source)

    def test_card_line_pairs_with_its_request(self):
        rows, stats = self._build([
            _user("开分支做第 3、4 条防护"),
            _assistant("[对齐门] 模式：standard｜S1：13→轻规划（不触碰 vibe）"),
        ])
        self.assertEqual([r["expected"] for r in rows], ["light_plan"])
        self.assertEqual(rows[0]["state"], "开分支做第 3、4 条防护")
        self.assertEqual(stats["paired_decisions"], 1)

    def test_detail_line_pairs_and_dedups_with_card_line(self):
        rows, stats = self._build([
            _user("推完，rebase 后合并"),
            _assistant(
                "S1 评估: 步骤(4) + 范围(1) + 不确定(1) + 失败代价(2) + 读码(2) = 10"
                " → 轻规划执行",
                "[对齐门] 模式：standard｜S1：10→轻规划（不触碰 vibe）",
            ),
        ])
        # Same decision written twice in one turn must collapse to one row.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expected"], "light_plan")
        self.assertGreaterEqual(stats["duplicate_pairs"], 1)

    def test_inconsistent_detail_line_is_dropped(self):
        rows, stats = self._build([
            _user("现在试"),
            _assistant("S1 评估: 步骤(5) + 范围(3) + 不确定(3) + 失败代价(3) + 读码(2) = 16\n→ 轻规划执行"),
        ])
        self.assertEqual(rows, [])
        self.assertEqual(stats["inconsistent"], 1)

    def test_heartbeat_between_request_and_decision_drops_decision(self):
        rows, stats = self._build([
            _user("都删掉并归档，然后发 v5.0.16"),
            _user("cd /Users/macmini/Desktop/vibeguide && vibe supervisor-beat --run-id run-1 --session-record /x.jsonl"),
            _assistant("[对齐门] 模式：standard｜S1：7→直接执行（不触碰 vibe）"),
        ])
        # The decision belongs to the heartbeat turn, not to the user request.
        self.assertEqual(rows, [])
        self.assertEqual(stats["machine_paired"], 1)

    def test_machine_request_is_not_a_case(self):
        rows, _ = self._build([
            _user("# OpenClaw Guardian 深度审计（每 48 小时） ## 角色 你是 OpenClaw Guardian"),
            _assistant("S1：11→轻规划（不触碰 vibe）"),
            _user("<system-reminder> You are operating in a git worktree </system-reminder>"),
            _assistant("S1：9→轻规划（不触碰 vibe）"),
            _user("Stop hook feedback: Johari end gate incomplete"),
            _assistant("S1：6→直接执行（不触碰 vibe）"),
        ])
        self.assertEqual(rows, [])

    def test_short_user_messages_are_kept(self):
        rows, _ = self._build([
            _user("跑完了，继续"),
            _assistant("S1：9→轻规划（不触碰 vibe）"),
            _user("同意续授权"),
            _assistant("S1：7→直接执行（不触碰 vibe）"),
        ])
        self.assertEqual(
            [(r["state"], r["expected"]) for r in rows],
            [("跑完了，继续", "light_plan"), ("同意续授权", "simple")],
        )

    def test_tool_result_decision_line_mints_nothing(self):
        rows, stats = self._build([
            _user("读一下 vibe-entry.md"),
            _tool_result("协议示例：评分后输出 S1：17→vibe 正式路由"),
        ])
        self.assertEqual(rows, [])
        self.assertEqual(stats["decision_lines"], 0)

    def test_conflicting_labels_drop_both_sides(self):
        rows, stats = self._build([
            _user("选1，先修 vibe"),
            _assistant("S1：5→直接执行（不触碰 vibe）"),
            _user("选1，先修 vibe"),
            _assistant("S1：12→轻规划（不触碰 vibe）"),
        ])
        self.assertEqual(rows, [])
        self.assertEqual(stats["conflicting_labels"], 1)
        self.assertEqual(stats["rows_dropped_conflict"], 2)

    def test_credentials_are_redacted_not_kept(self):
        rows, stats = self._build([
            _user("对熟小麦生产库做全链路交易核账。麦主手机号 18333383657，host=rm-bp14f50v736tvdqe7to.mysql.rds.aliyuncs.com:3306 user=employ pass=Employ123456"),
            _assistant("S1：15→轻规划（不触碰 vibe）"),
        ])
        self.assertEqual(len(rows), 1)
        state = rows[0]["state"]
        self.assertNotIn("18333383657", state)
        self.assertNotIn("Employ123456", state)
        self.assertNotIn("rm-bp14f50v736tvdqe7to", state)
        self.assertIn("[REDACTED:phone]", state)
        self.assertEqual(stats["rows_redacted"], 1)

    def test_orphan_decision_without_any_user_message_is_counted(self):
        rows, stats = self._build([
            _assistant("S1：11→轻规划（不触碰 vibe）"),
        ])
        self.assertEqual(rows, [])
        self.assertEqual(stats["orphans"], 1)

    def test_question_wording_is_frozen(self):
        rows, _ = self._build([
            _user("合并 #211"),
            _assistant("S1：3→直接执行（不触碰 vibe）"),
        ])
        self.assertEqual(rows[0]["question"], build_from_history.CANONICAL_QUESTION)
        self.assertEqual(sorted(rows[0]["question"]["criteria"]), ["complex", "light_plan", "simple"])


if __name__ == "__main__":
    unittest.main()
