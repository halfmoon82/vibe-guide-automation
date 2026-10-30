"""Phase 3 contract: the PRD-guide protocol is shipped, materialized, and enforced.

The protocol is what the host agent reads to run the PRD conversation; vibe
owns validation.  These tests pin (1) the protocol's embedded schema to
node_spec.PRODUCT_SPEC_FIELDS so the document cannot drift from the code,
(2) init materializing it into the project, (3) unresolved
`needs_confirmation` items blocking publish, and (4) goal traceability rows
landing in planning-brief.md.
"""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
COMPLEX_REQUEST = "设计并实现支付系统，迁移数据、集成接口、编写测试并部署"
CAPABILITIES = {
    "schema_version": 1,
    "adapter_id": "claude-code",
    "facts": {name: True for name in (
        "claude-code.agent", "claude-code.shell", "claude-code.subprocess", "claude-code.worktree",
        "claude-code.visible_task.create", "claude-code.visible_task.enter",
        "claude-code.visible_task.resume", "claude-code.visible_task.wait",
    )},
    "provenance": "test fixture",
    "project_id": "pmpathprobe",
}


def _product_spec():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ProtocolShippingTests(unittest.TestCase):
    def test_every_shipped_rule_block_opens_with_its_own_heading(self):
        """New rule blocks are registered here, and headings identify them.

        A section is tracked by the heading on its first line -- which sections
        the proposal holds, which the reviewer has been offered.  A block with no
        first line raises on that lookup, and two blocks sharing a heading
        collapse into one entry, permanently suppressing the second.
        """
        from vibe_guide.scanner import AGENTSMD_BLOCKS
        headings = [block.splitlines()[0].strip() for block in AGENTSMD_BLOCKS]
        for heading in headings:
            self.assertTrue(heading.startswith("## "), heading)
        self.assertCountEqual(headings, set(headings), "two blocks share a heading")

    def test_protocol_schema_example_matches_node_spec_schema(self):
        from vibe_guide.node_spec import PRODUCT_SPEC_FIELDS
        from vibe_guide.protocols import load_protocol, protocol_schema_example
        text = load_protocol("prd-guide")
        self.assertEqual(protocol_schema_example(text), PRODUCT_SPEC_FIELDS)

    def test_protocol_lists_every_engineering_field_as_forbidden(self):
        from vibe_guide.node_spec import ENGINEERING_FIELDS
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        for group in ENGINEERING_FIELDS.values():
            for name in group:
                self.assertIn("`{}`".format(name), text, name)

    def test_protocol_is_shipped_as_package_data(self):
        from vibe_guide import protocols
        self.assertTrue((Path(protocols.__file__).parent / "prd-guide.md").is_file())
        setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn("vibe_guide.protocols", setup_text)
        self.assertRegex(setup_text, r'"vibe_guide\.protocols":\s*\["\*\.md"\]')

    def test_protocol_documents_environment_facts_rule(self):
        """The closure rule for environment_facts stays true to the code.

        The protocol is the only place a host agent learns when the facts are
        mandatory and how `source` is read, so the trigger list and the
        discrimination tokens are asserted against the module that enforces
        them rather than merely grepped for.
        """
        from vibe_guide.contracts import THIRD_PARTY_UI_LIBRARY_MARKERS, _REPO_PATH_SUFFIXES
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        self.assertIn("environment_facts", text)
        self.assertIn("THIRD_PARTY_UI_LIBRARY_MARKERS", text)
        for marker in THIRD_PARTY_UI_LIBRARY_MARKERS:
            self.assertIn("`{}`".format(marker), text, marker)
        for suffix in _REPO_PATH_SUFFIXES:
            self.assertIn("`{}`".format(suffix), text, suffix)
        for token in ("://", "verified_at", "fact", "source"):
            self.assertIn(token, text, token)

    def test_protocol_mentions_every_cli_command_it_relies_on(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        for command in ("vibe attest", "--from-prd", "vibe authorize", "vibe monitor", "vibe status", "vibe resume"):
            self.assertIn(command, text, command)
        for marker in ("user_confirmed", "system_inferred", "needs_confirmation", "unverified"):
            self.assertIn(marker, text, marker)


class ScopeRuleSectionTests(unittest.TestCase):
    """§4/§6.2/§7 must not present a file-list change as a product decision.

    Issue #145: the same word "范围" meant both the product scope (a product
    manager boundary) and the authorization card's file list (an engineering
    detail), so a one-file correction asked the product manager to re-authorize.
    The protocol now separates them and names the approved criterion; these
    tests pin the wording to the code that implements it.
    """

    PRODUCT_SCOPE_RULE = (
        "修改后，PRD 里每条验收例子的判定结果是否改变——不变就由监工自决并留痕，"
        "变了才问产品经理；另外两类无论如何都问：新增第三方依赖（发布/安全对外承诺）、"
        "执行模式从「可见可进入的任务」降级为「后台不可见」。"
    )

    def _sections(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        return text, self._section(text, "## 4. 拆节点"), \
            self._section(text, "## 6. 监工：守护程序"), \
            self._section(text, "## 7. 什么时候才能打断产品经理")

    def _section(self, text, heading):
        """Return one numbered section, up to the next heading at the same level.

        Slicing to the next ``## `` keeps an assertion from being satisfied by
        a later section; a renamed heading fails here with a readable message
        instead of an opaque ValueError from ``str.index``.
        """
        self.assertIn(heading, text, "protocol section heading moved: " + heading)
        start = text.index(heading)
        rest = text[start + len(heading):]
        end = rest.find("\n## ")
        return heading + (rest if end == -1 else rest[:end])

    def test_file_list_is_an_engineering_boundary_not_a_product_one(self):
        _text, section_4, _section_6, _section_7 = self._sections()
        self.assertIn("工程文件清单", section_4)
        self.assertIn("不是产品范围", section_4)
        self.assertNotIn("其他文件漏列仍必须请产品经理授权", section_4)

    def test_both_scope_criteria_are_named(self):
        _text, section_4, _section_6, _section_7 = self._sections()
        self.assertIn("判据一", section_4)
        self.assertIn("判据二", section_4)
        # Criterion two is opt-in per project: the protocol must name the very
        # config field the monitor reads, and must say that it is conditional.
        # Naming the field without the condition reads as "always available",
        # which sends the supervisor back to the product manager for a file
        # that the default configuration would reject.
        from vibe_guide.config import FIELD_ACCEPTANCE_SCOPE_RULE
        self.assertIn(FIELD_ACCEPTANCE_SCOPE_RULE, section_4)
        self.assertIn(
            "只在项目于 `.vibe/config.json` 写入 `{}: true` 后生效".format(
                FIELD_ACCEPTANCE_SCOPE_RULE
            ),
            section_4,
        )
        self.assertIn("未开启的项目一律按判据一处理", section_4)
        # Criterion two is limited to low-risk files; without that qualifier the
        # prose promises more than `_approved_scope_rule` allows.
        self.assertIn("低风险源码或文档", section_4)
        # And criterion one is described truthfully: a registered
        # `auto_scope_paths` entry is the product manager's own pre-approval,
        # so that branch does not re-check the dependency/sensitive lists.
        self.assertIn("不再逐条检查", section_4)
        # Matching is exact; a reader who registers a glob would otherwise
        # expect it to work and see nothing expand.
        self.assertIn("不支持通配符", section_4)
        # The pointer has to name the function that actually decides the
        # suffix whitelist and the exclusions, not just half of it.
        self.assertIn("planner._approved_scope_rule", section_4)
        # Files outside both criteria still need a separate authorization.
        self.assertIn("请产品经理单独授权", section_4)

    def test_scope_expansion_payload_shape_matches_the_code(self):
        """#146 residual: the claim shape was implemented but never documented.

        The documented key list is parsed and compared as a set, so renaming a
        key, adding a sixth key or spelling one in another case fails here.
        Substring matching over the whole section would survive all three
        (``files`` also appears elsewhere).
        """
        from vibe_guide.planner import _SCOPE_EXPANSION_KEYS
        _text, section_4, _section_6, _section_7 = self._sections()
        marker = "**恰好五个键**："
        self.assertIn(marker, section_4)
        sentence = section_4.split(marker, 1)[1].split("。", 1)[0]
        documented = set(re.findall(r"`([^`]+)`", sentence)) - {"true", "false"}
        self.assertEqual(documented, set(_SCOPE_EXPANSION_KEYS))
        self.assertIn("vibe scope-expand", section_4)
        # The call is only accepted on a settled node; the prose has to say so
        # or a supervisor tries it mid-development and gets a hard error.
        self.assertIn("已验收、当前没有活跃任务", section_4)

    def test_a_bad_path_rejects_the_whole_request(self):
        """No partial expansion: one rejected path must not let the rest through."""
        from vibe_guide.planner import resolve_scope_expansion
        _text, section_4, _section_6, _section_7 = self._sections()
        self.assertIn("不做部分补入", section_4)
        # The prose claim is checked against the code it describes, in both
        # accepted shapes: the legacy path list and the five-key claim.
        self.assertIsNone(
            resolve_scope_expansion(
                ["tests/test_a.py", "src/app.css"], authorized_files=[], node_files=()
            )
        )
        self.assertIsNone(
            resolve_scope_expansion(
                ["tests/test_a.py", "tests/test_a.py"], authorized_files=[], node_files=()
            )
        )
        claim = {
            "files": ["tests/test_a.py", "src/app.css"],
            "acceptance_examples_unchanged": True,
            "adds_third_party_dependency": False,
            "visibility_downgrade": False,
            "reason": "mechanical follow-up",
        }
        self.assertIsNone(
            resolve_scope_expansion(
                claim, authorized_files=[], node_files=(), acceptance_rule_approved=True
            )
        )

    def test_release_is_not_a_second_human_approval_gate(self):
        _text, _section_4, section_6, _section_7 = self._sections()
        self.assertIn("没有第二道人工审批门", section_6)
        self.assertNotIn("放行仍然是人的决定", section_6)
        # The paragraph still has to say the gate does not verify the claim.
        self.assertIn("这道门只记账", section_6)

    def test_interrupt_criterion_is_stated_verbatim(self):
        _text, _section_4, _section_6, section_7 = self._sections()
        self.assertIn(self.PRODUCT_SCOPE_RULE, section_7)

    def test_interrupt_criterion_carries_its_enabling_condition(self):
        """§7 is what the supervisor reads before interrupting; it must not
        imply that criterion two works without the project's opt-in."""
        from vibe_guide.config import FIELD_ACCEPTANCE_SCOPE_RULE
        _text, _section_4, _section_6, section_7 = self._sections()
        self.assertIn(FIELD_ACCEPTANCE_SCOPE_RULE, section_7)
        self.assertIn("只在项目开启", section_7)
        self.assertIn("未开启的项目按判据一处理", section_7)
        # A file outside criterion one still needs a separate authorization;
        # saying otherwise would leave the supervisor with no executable exit.
        self.assertIn("文件本身越界", section_7)
        self.assertIn("会问产品经理", section_7)
        self.assertNotIn("登记为后续问题", section_7)


class MailboxSectionTests(unittest.TestCase):
    """The mailbox section must stay true to the code that serves it.

    A monitored run stops at the mailbox and waits: `vibe` writes a request and
    only advances once a host agent writes the matching result back.  Nothing
    in the package can perform that step -- it needs the desktop tools the
    session itself holds -- so the protocol is the only place that step is
    specified.  Prose alone would rot silently, so each fact below is asserted
    against the module that produces it rather than merely grepped for.
    """

    def protocol(self):
        from vibe_guide.protocols import load_protocol
        return load_protocol("prd-guide")

    def test_protocol_embeds_the_generated_single_line_heartbeat_command(self):
        from vibe_guide.supervisor import heartbeat_command
        text = self.protocol()
        command = heartbeat_command("<run-id>")
        self.assertNotIn("\n", command)
        self.assertIn(command, text)
        self.assertIn("逐字用 vibe 打印的", text)
        self.assertIn("supervisor-beat", text)
        opening = text[text.index("### 6.0"):text.index("### 6.1")]
        self.assertEqual(opening.count(command), 1)
        self.assertNotIn("vibe 监工心跳", opening)
        self.assertNotIn("vibe supervisor-preflight", opening)

    def section(self):
        """The mailbox section alone.

        Assertions are scoped to the row or block that carries the fact.  A
        bare `assertIn` over the whole document is satisfied by any other line
        that happens to contain the same word, so eleven of fifteen deliberate
        drifts slipped past the first version of these tests.
        """
        text = self.protocol()
        start = text.index("## 6. 监工：守护程序")
        return text[start:text.index("\n## 7. ", start)]

    def subsection(self, heading):
        """One `### 6.x` block, so a row is matched in the table that owns it.

        `create` heads a row in both the write-back table and the platform
        table, so searching the whole section conflates the two.
        """
        section = self.section()
        start = section.index("### " + heading)
        end = section.find("\n### ", start)
        return section[start:] if end == -1 else section[start:end]

    def row(self, heading, label):
        """The table rows in `heading` whose first cell is `label`."""
        prefix = "| `{}`".format(label)
        rows = [
            line for line in self.subsection(heading).splitlines()
            if line.startswith(prefix)
        ]
        self.assertTrue(rows, "no row for {} in {}".format(label, heading))
        return rows

    def test_protocol_write_back_shapes_match_the_runner_contract(self):
        """Each operation's payload keys, checked in that operation's own row.

        The runner reads a different key per operation and silently discards a
        result that lacks it, so a host agent cannot recover the shape from the
        request.  `create` is the one that already went wrong once: an earlier
        draft sent `sessionId`, the desktop tool's own field name, which the
        runner rejects as "no task identity".
        """
        source = (ROOT / "vibe_guide" / "runners" / "provider_action.py").read_text(encoding="utf-8")
        accepted = re.search(
            r'task_id = binding_data\.get\("(\w+)"\) or binding_data\.get\("(\w+)"\)\n'
            r'\s*host = binding_data\.get\("(\w+)"\) or binding_data\.get\("(\w+)"\)',
            source,
        )
        self.assertIsNotNone(accepted, "create binding keys no longer read this way")
        create = " ".join(self.row("6.2 信箱回写", "create"))
        # Quoted inside the JSON example or backticked as an accepted alias --
        # either way a key token, never bare prose.
        for key in accepted.groups():
            self.assertRegex(create, r'[`"]{}[`"]'.format(key), key)
        self.assertNotIn("sessionId", create, create)

        self.assertIn('`{"located": true}`', " ".join(self.row("6.2 信箱回写", "locate")))
        self.assertIn('"visible": true', " ".join(self.row("6.2 信箱回写", "visibility")))
        self.assertIn('"direct_enter": true', " ".join(self.row("6.2 信箱回写", "visibility")))

        # `resume` reads `resumed` and never looks at `binding`; a create-shaped
        # payload has no `resumed`, so the round reports visibility_unknown.
        self.assertIn('"resumed": true', " ".join(self.row("6.2 信箱回写", "resume")).lower())
        self.assertRegex(source, r'operation != "resume" or result\.get\("resumed"\) is not True')

        wait_rows = " ".join(self.row("6.2 信箱回写", "wait"))
        self.assertIn('"status": "timeout"', wait_rows)
        self.assertIn("cursor", wait_rows)
        # Terminal wait needs status *and* event, from fixed sets, and the
        # event name differs by role.  Dropping either stalls the run.
        for token in ('"status": "completed"', '"event": "complete"', '"event": "accepted"'):
            self.assertIn(token, wait_rows, token)

    def test_terminal_wait_payload_passes_the_delivery_evidence_gate(self):
        """The documented terminal payload, run through the gate that reads it.

        A complex plan sets `execution_engine` to `vibeguide_monitor`, which
        arms `evaluate_delivery_evidence` on every developer `complete`.  The
        unit fixture in `test_claude_code_dispatch` has no `complexity_band`,
        so that gate is dormant there and an ablation against it cannot see
        these fields at all -- which is how an earlier draft came to document
        `delivery_evidence` as absent and `cursor` as optional.  This asserts
        the payload the protocol prints, against the gate itself.
        """
        import json as _json
        from vibe_guide.evidence import evaluate_delivery_evidence
        # Every JSON object in the subsection, wherever it is printed: in a
        # table cell, a fenced block, indented prose.  Keying on the table row
        # and on the literal "completed" made a *true* document red whenever it
        # was reformatted, or when the equally valid `"complete"` was used.
        subsection = self.subsection("6.2 信箱回写")
        payloads = []
        for chunk in re.findall(r"\{.*?\}(?=`|\s|$)", subsection, re.S):
            try:
                value = _json.loads(chunk)
            except ValueError:
                continue
            if isinstance(value, dict):
                payloads.append(value)
        terminal = [p for p in payloads if p.get("event") in {"complete", "accepted"}]
        self.assertEqual(
            len(terminal), 2,
            "expected one terminal payload per role; parsed {} from {} objects".format(
                len(terminal), len(payloads)
            ),
        )
        binding = {
            "task_id": "sess_probe", "host": "probe-host",
            "worktree": ".worktrees/probe", "branch": "node/probe",
        }
        def gate(payload):
            """As the monitor calls it: the evidence comes from one key.

            `monitor.py` passes `event.data.get("delivery_evidence")`, which is
            why nesting matters -- marks spread across the payload's top level
            are never looked at, so they fail exactly like omitting them.
            """
            return evaluate_delivery_evidence(
                {"status": "DELIVERED"},
                {**binding, "cursor": payload.get("cursor")},
                payload.get("delivery_evidence"),
            )

        self.assertIn(
            'event.data.get("delivery_evidence")',
            (ROOT / "vibe_guide" / "monitor.py").read_text(encoding="utf-8"),
        )
        developer = next(p for p in terminal if p.get("event") == "complete")
        # On the servicer path the cursor reaches the binding only from this
        # payload (ProviderActionRunner._wait_result), so omitting it fails the
        # gate; only a worker-deliver receipt stands in for it.
        self.assertIn("cursor", developer)
        self.assertEqual(gate(developer).status, "DELIVERED", gate(developer).reasons)
        flat = {k: v for k, v in developer.items() if k != "delivery_evidence"}
        flat.update(developer["delivery_evidence"])
        self.assertEqual(
            gate(flat).status, "blocked_unknown",
            "flattened delivery marks must not read as evidence",
        )
        without_cursor = {k: v for k, v in developer.items() if k != "cursor"}
        self.assertIn("current cursor is missing", gate(without_cursor).reasons)
        # The reviewer's own requirement is a separate check in the monitor.
        reviewer = next(p for p in terminal if p.get("event") == "accepted")
        self.assertIn("evidence", reviewer)
        monitor = (ROOT / "vibe_guide" / "monitor.py").read_text(encoding="utf-8")
        self.assertIn("review acceptance has no registered P0-P2 clearance evidence", monitor)

    def test_protocol_prose_states_the_gate_requirements_it_explains(self):
        """The sentences next to the payload rows that say what the gate enforces.

        The row assertions pin what a host agent should copy; this pins the
        qualifiers on the same rows.  Dropping "嵌套对象" or "摊平无效" left every
        payload assertion green while hiding what the gate reads.  The long
        per-claim prose of the old runbook was deliberately replaced by these
        row notes and by the supervisor card.
        """
        section = self.section()
        self.assertIn("（嵌套对象，三键齐全，摊平无效）", section)
        reviewer = " ".join(self.row("6.2 信箱回写", "wait"))
        self.assertIn('"event": "accepted", "evidence"', reviewer)
        # #235: background dispatch now creates a missing node worktree itself,
        # while visible dispatch still needs the directory to exist.  Both halves
        # are pinned so neither claim can drift away from the code alone.
        contract = self.subsection("6.3 派发合同")
        self.assertIn("后台派发（`claude-code-background`）由 vibe 在派发前自建缺失的节点 worktree", contract)
        self.assertIn("可见派发仍需 worktree 先存在", contract)
        # The two halves a supervisor acts on, and the label it must not wait
        # for on paths that never record it (review finding on PR #333).
        self.assertIn("vibe 不代建", contract)
        self.assertIn("worktree_mismatch", contract)
        self.assertIn("分支已存在", contract)
        self.assertIn("干净、在合同 `branch` 与 `base_sha` 上的 git worktree", contract)
        # The known gap, pinned so it cannot disappear from the docs unnoticed:
        # daemon and post-watch beats never record worktree_preflight.
        self.assertIn("守护程序续跑都不落这个标记", contract)
        self.assertNotIn("vibe 不替你建", contract)

    def test_protocol_states_the_cursor_and_recovery_rules(self):
        """Two rules whose absence is silent, so nothing else would notice.

        An empty cursor discards the whole result rather than risking a double
        read, and a rejected write-back is recovered through a freshly emitted
        request -- rewriting the same action_id does nothing, because a request
        that already has a result file is never re-read.
        """
        section = self.section()
        source = (ROOT / "vibe_guide" / "runners" / "provider_action.py").read_text(encoding="utf-8")
        self.assertIn("provider cursor is invalid", source)
        self.assertIn("`cursor` 给就必须是非空字符串", section)
        self.assertIn("不知道就整个不给", section)
        self.assertIn(
            "回写被拒后不要重写同一 `action_id`；`vibe resume` 会发新的 `create`（`generation` 加一）",
            section,
        )
        # The self-report exception: without it a servicer that read only the
        # rule above would forge a cursor for a self-report.
        self.assertIn("不要再手工回写同代 `wait`", section)

    def test_protocol_states_the_per_platform_dispatch_boundary(self):
        """Unattended dispatch is a platform fact, not a vibe feature.

        Both providers are wired, but only one creates a session without a
        human click.  An open-source user reading this protocol has to learn
        that before they plan around "authorize once and walk away".
        """
        from vibe_guide.providers import CLAUDE_CODE_PROVIDER, CODEX_PROVIDER
        from vibe_guide.runners.provider_action import NATIVE_TOOL_MAP
        section = self.section()
        for provider, tools in NATIVE_TOOL_MAP.items():
            self.assertIn(provider, section, provider)
            for operation, tool in tools.items():
                self.assertIn(tool, section, "{}/{}".format(provider, operation))
        # Which side is unattended is the whole point of the table, and
        # swapping the two providers left every name still present.  Codex
        # creates without a click; Claude Code needs one per node.
        unattended = section[section.index("- **Codex 本地桌面**"):]
        unattended = unattended[:unattended.index("- **Claude Code 桌面**")]
        self.assertIn("无人值守", unattended)
        self.assertNotIn("无人值守", section[section.index("- **Claude Code 桌面**"):])
        # *Why* Codex is the unattended one, not just that it is labelled so.
        # Negating this clause to "有审批门" left every other assertion green,
        # which would have inverted the one fact the table exists to convey.
        self.assertIn("`create_thread` 没有审批门", unattended)
        self.assertIn(CODEX_PROVIDER, section)
        self.assertIn(CLAUDE_CODE_PROVIDER, section)

    def test_protocol_does_not_promise_unattended_claude_code_dispatch(self):
        """The measured Claude Code behaviour has to survive a doc edit.

        `spawn_task` proposes a task and shows the user a card; it returns a
        `task_id`, never a session.  Anyone who writes "fully automatic" back
        into this section has contradicted what was measured on 2026-09-18.
        """
        text = self.protocol()
        self.assertIn("无人值守", text)
        # Each fact is asserted on its own.  A window-wide regex passes as long
        # as any nearby sentence mentions a click, so deleting the one that
        # states the measured behaviour left it green.
        claude = text[text.index("`ccd_session__spawn_task` 只是"):]
        claude = claude[:claude.index("\n")]
        # One string, because the facts only mean anything together: the call
        # only proposes, a human has to act, and the caller is left without a
        # session id.
        self.assertIn(
            "只是**提议**一个任务，界面上出一张卡片，"
            "**需要用户点一下**才创建会话，调用方拿不到 `sessionId`",
            claude,
        )
        # The per-node confirmation point is told in the closing paragraph.
        self.assertIn("每个节点都需要他点一下确认", text)
        for promise in ("自动创建会话", "不需要确认"):
            self.assertNotIn(promise, claude, promise)
        self.assertNotIn("全自动", claude.replace("把它当\"全自动\"会一直卡在", ""))


class PublishSectionTests(unittest.TestCase):
    """§1 and §5.3 must describe the publish path the code actually takes.

    Issue #97: the protocol told agents to publish under the draft's id (which
    failed), not to pass --s1 (which silently downgraded the plan), and to pick
    the remote Git switch at authorization (which cannot change it).  Each
    assertion is scoped to the line that carries the instruction.
    """

    def protocol(self):
        from vibe_guide.protocols import load_protocol
        return load_protocol("prd-guide")

    def section(self, heading, next_heading):
        text = self.protocol()
        start = text.index(heading)
        return text[start:text.index(next_heading, start)]

    def line(self, section, needle):
        lines = [line for line in section.splitlines() if needle in line]
        self.assertEqual(len(lines), 1, "expected one line with {!r}: {}".format(needle, lines))
        return lines[0]

    def test_s1_rule_says_publish_inherits_the_draft_score(self):
        rule = self.line(self.section("## 1. 入口判定", "\n## 2. "), "不要自己算 `--s1`")
        self.assertIn("发布时沿用草案", rule)
        self.assertIn("绝不反向降级", rule)
        self.assertNotIn("不要传 `--s1`", self.protocol())

    def test_publish_step_names_the_draft_replacement_and_its_limit(self):
        section = self.section("### 5.3 ", "\n## 6. ")
        rule = self.line(section, "同一个 `plan_id`")
        self.assertIn("替换这份草案", rule)
        self.assertIn("已发布或已授权的计划不会被覆盖", rule)

    def test_publish_step_tells_how_to_recover_a_parked_draft(self):
        """Where a draft is left when a swap is interrupted comes from the code."""
        import inspect
        from vibe_guide import publish
        source = inspect.getsource(publish._replace_draft)
        self.assertIn('prefix="." + destination.name + ".draft."', source)
        self.assertIn('parked = holding / "draft"', source)
        self.assertIn("draft could not be restored", source)
        rule = self.line(self.section("### 5.3 ", "\n## 6. "), "draft could not be restored")
        self.assertIn("替换中途被打断（如 Ctrl-C）", rule)
        self.assertIn("以 reason 里给出的路径为准", rule)
        self.assertIn("`.vibe/plans/.<plan_id>.draft.*/draft`", rule)
        # Most interrupts land before the draft is moved; that branch first.
        self.assertIn("①找不到：草案没被挪动，直接按原命令重新发布", rule)
        self.assertIn("②找到了且 `.vibe/plans/<plan_id>` 不存在：把它整个挪回这个位置，再按原命令重新发布", rule)
        self.assertIn("③找到了且 `.vibe/plans/<plan_id>` 已存在就不要覆盖", rule)
        # A card only proves the id holds *a* publication, not this one.
        self.assertIn("也不要当成本次发布成功", rule)
        self.assertNotIn("照 `status: ok` 往下走", rule)
        self.assertIn("从挪开的草案里取回产品 spec", rule)
        self.assertIn("告诉产品经理这个编号已被占用", rule)
        # One string: without --s1 a short request falls back to its text
        # score and the republish is refused.
        self.assertIn("再换一个新的 `plan_id` 重新发布，`--s1` 照挪开草案 `plan.json` 里 `route_result.dimensions`", rule)
        self.assertIn("`route_result.dimensions` 的 `steps,domains,uncertainty,failure_cost,toolchain` 依次填", rule)
        self.assertIn("命中多个时别猜：把每个目录的修改时间和其中 `plan.json` 的状态列给产品经理，请他选", rule)
        # Why the old id can hold this very publication: see
        # test_an_interrupt_after_the_swap_leaves_this_publication_under_the_old_id.
        self.assertIn("旧编号下那份计划原样留着", rule)
        self.assertIn("不念它的卡、不对它 `vibe authorize` 或 `vibe monitor`，授权和开工只用新编号", rule)
        self.assertIn("可能是本次被打断的发布，也可能是别的会话的", rule)
        self.assertIn("清不清理由产品经理决定", rule)
        self.assertIn("挪开的草案移到废纸篓", rule)
        self.assertIn("空隐藏目录可以删掉", rule)
        # The field order the protocol names is the order draft_s1 reads.
        from vibe_guide import session_entry
        self.assertIn('("steps", "domains", "uncertainty", "failure_cost", "toolchain")',
                      inspect.getsource(session_entry.draft_s1))

    def test_spec_location_depends_on_whether_the_id_has_a_draft(self):
        """§5.1: a spec kept under a new id's directory makes its publish fail."""
        import inspect
        from vibe_guide import cli
        # The code fact: an existing non-draft directory is never published over.
        source = inspect.getsource(cli._publish_plan)
        self.assertIn("if replaceable_draft_extras(destination) is None:\n            raise FileExistsError(\"plan already exists\")", source)
        section = self.section("### 5.1 ", "\n### 5.2 ")
        same = self.line(section, "同一个 `plan_id` 发布")
        self.assertIn("`.vibe/plans/<plan_id>/product-spec.json`", same)
        self.assertIn("随草案一起保留", same)
        fresh = self.line(section, "没有草案的新 `plan_id`")
        self.assertIn("放在 `.vibe/plans` 之外", fresh)
        self.assertIn("建议项目根目录的 `product-spec.json`", fresh)
        self.assertIn("§5.3 改远端 Git 开关、恢复分支③", fresh)
        why = self.line(section, "不要事先建")
        self.assertIn("`.vibe/plans/<新编号>/`", why)
        self.assertIn("vibe 只替换草案目录", why)
        self.assertIn("`plan already exists`", why)
        self.assertNotIn("也可放项目内任意位置", self.protocol())
        publish = self.section("### 5.3 ", "\n## 6. ")
        change = self.line(publish, "`vibe authorize` 改不了")
        self.assertIn("把产品 spec 复制到 `.vibe/plans` 之外（见 §5.1）", change)
        recover = self.line(publish, "draft could not be restored")
        self.assertIn("从挪开的草案里取回产品 spec 放到 `.vibe/plans` 之外（见 §5.1）", recover)

    def test_remote_git_switch_is_chosen_before_publishing(self):
        from vibe_guide.cli import _parser
        from vibe_guide.node_spec import PRODUCT_SPEC_FIELDS
        self.assertIn("remote_git_actions?", PRODUCT_SPEC_FIELDS)
        # The code fact the prose rests on: authorize takes no such switch.
        dests = {action.dest for action in _parser()._actions}
        self.assertFalse({dest for dest in dests if "remote" in dest}, dests)
        section = self.section("### 5.3 ", "\n## 6. ")
        before_publish = section[:section.index("```bash")]
        rule = self.line(before_publish, "`remote_git_actions`")
        self.assertIn("发布前", rule)
        self.assertIn("缺省 `deny`", rule)
        change = self.line(section, "`vibe authorize` 改不了")
        self.assertIn("新的 `plan_id` 重新发布", change)
        # The draft is gone after the first publish, so the new id has
        # nothing to inherit; the first publish hands its S1 back instead.
        self.assertIn("首次发布结果里的 `s1`", change)
        self.assertIn("--s1 <s1>", change)
        for stale in ("唯一要选的是允许还是禁止远端 Git 动作", "选定远端 Git 动作后"):
            self.assertNotIn(stale, self.protocol(), stale)

    def test_card_reading_separates_worker_envelope_from_user_grant(self):
        section = self.section("### 5.3 ", "\n## 6. ")
        rule = self.line(section, "`excluded_actions`")
        self.assertIn("`allowed_actions`", rule)
        self.assertIn("开发任务自己永远不推送", rule)
        self.assertIn("进入授权范围", rule)
        self.assertIn("vibe 只在独立终审通过后校验并记录，不代为执行", rule)
        self.assertNotIn("监工可以在独立终审通过后执行", self.protocol())
        # The code facts behind "records, never executes": the monitor stamps
        # remote Git actions as not performed, and the only merge gate needs
        # merge_local/merge_remote, which the published card never carries.
        import inspect
        from vibe_guide import authorization, change_requests
        self.assertIn('"external_execution": "not_performed"', inspect.getsource(change_requests))
        self.assertIn('{"merge_local", "merge_remote"}', inspect.getsource(authorization.can_auto_merge))
        # The worker envelope is fixed, so it lists push/merge under deny too.
        from vibe_guide.authorization import _EXCLUDED_ACTIONS, _REMOTE_GIT_ACTIONS_SCOPE
        self.assertTrue({"push", "merge"} <= set(_EXCLUDED_ACTIONS))
        self.assertIn("`excluded_actions` 照样列着推送/合并", rule)
        self.assertFalse({"merge_local", "merge_remote"} & set(_REMOTE_GIT_ACTIONS_SCOPE))
        for action in _REMOTE_GIT_ACTIONS_SCOPE:
            from vibe_guide.authorization import _ALLOWED_ACTIONS
            self.assertNotIn(action, _ALLOWED_ACTIONS, action)
        self.assertIn("选 `deny` 时 `allowed_actions` 不含任何远端 Git 动作", rule)

    def test_protocol_lists_the_downgrade_refusal_reason_the_cli_emits(self):
        root = Path(tempfile.mkdtemp(prefix="issue97-doc-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "spec.json").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        result = run_cli(
            ["plan", "--json", "--request", "发布 V4.10 计划", "--plan-id", "p", "--from-prd", "spec.json"], root,
        )
        self.assertEqual(result.payload.get("status"), "blocked", result.payload)
        code = result.payload["reason"].split(":", 1)[0]
        row = self.line(self.section("### 5.3 ", "\n## 6. "), "`{}`".format(code))
        self.assertIn("`status: blocked`", row)
        self.assertIn("`--s1`", row)


class _ProjectCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v45-prd-guide-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)

    def init(self):
        result = run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        return result

    def write_capabilities(self):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(json.dumps(CAPABILITIES), encoding="utf-8")

    def _write_pre_release_proposal(self, proposal):
        """Leave behind what an earlier release's init would have written.

        Only the capability block existed then, and nothing recorded which
        headings had been shown -- so a later init can tell "this predates the
        section" from "the reviewer removed it".  Editing a current proposal
        would not reproduce that: on disk the two look identical.
        """
        from vibe_guide.scanner import CAPABILITY_RULES
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_text(
            CAPABILITY_RULES.rstrip("\n") + "\n\n<!-- 评审备注：保留 -->\n",
            encoding="utf-8",
        )

    def plan_from_prd(self, spec, plan_id="pm-plan"):
        (self.root / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return run_cli(
            ["plan", "--json", "--request", COMPLEX_REQUEST, "--plan-id", plan_id, "--s1", "5,5,5,5,5", "--from-prd", "product-spec.json"],
            self.root,
        )


class PrintProtocolTests(_ProjectCase):
    def test_plan_print_protocol_outputs_the_shipped_text_without_touching_the_project(self):
        from vibe_guide.protocols import load_protocol
        before = sorted(str(p) for p in self.root.rglob("*"))
        result = run_cli(["plan", "--print-protocol"], self.root)
        self.assertEqual(result.exit_code, 0, result.text)
        self.assertEqual(result.text, load_protocol("prd-guide"))
        as_json = run_cli(["plan", "--print-protocol", "--json"], self.root)
        self.assertEqual(as_json.payload.get("status"), "ok")
        self.assertEqual(as_json.payload.get("protocol"), load_protocol("prd-guide"))
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)

    def test_print_protocol_stays_read_only_on_an_initialized_project(self):
        """The read-only claim must hold where agents actually run it.

        Session screening fires for any project whose state.json exists, so an
        initialized project used to get a session-gates.json write from a
        command that only prints a document.
        """
        from vibe_guide.protocols import load_protocol
        self.init()
        gates = self.root / ".vibe" / "session-gates.json"
        gates.unlink(missing_ok=True)
        before = sorted(str(item) for item in self.root.rglob("*"))
        result = run_cli(["plan", "--print-protocol"], self.root)
        self.assertEqual(result.exit_code, 0, result.text)
        self.assertEqual(result.text, load_protocol("prd-guide"))
        self.assertFalse(gates.exists(), "--print-protocol must not open a session gate")
        self.assertEqual(sorted(str(item) for item in self.root.rglob("*")), before)


class InitMaterializationTests(_ProjectCase):
    def test_init_materializes_prd_guide_proposal(self):
        from vibe_guide.protocols import PRD_GUIDE_PROPOSAL_RELATIVE, load_protocol
        first = self.init()
        skill = self.root / PRD_GUIDE_PROPOSAL_RELATIVE
        self.assertTrue(skill.is_file(), PRD_GUIDE_PROPOSAL_RELATIVE)
        self.assertEqual(skill.read_text(encoding="utf-8"), load_protocol("prd-guide"))
        self.assertIn(PRD_GUIDE_PROPOSAL_RELATIVE, first.payload.get("paths", []))
        agentsmd_proposal = (self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md").read_text(encoding="utf-8")
        self.assertIn(PRD_GUIDE_PROPOSAL_RELATIVE, agentsmd_proposal)
        self.assertIn("## Capability and Tool Truth", agentsmd_proposal)
        skill.write_text("user edited", encoding="utf-8")
        second = self.init()
        self.assertEqual(skill.read_text(encoding="utf-8"), "user edited")
        self.assertNotIn(PRD_GUIDE_PROPOSAL_RELATIVE, second.payload.get("paths", []))

    def test_reinit_notes_a_stale_prd_guide_copy_without_rewriting_it(self):
        """The copy is never rewritten, so an old project keeps an old guide
        silently unless init says so -- the same note vibe-entry gets."""
        from vibe_guide.protocols import PRD_GUIDE_PROPOSAL_RELATIVE, load_protocol
        self.init()
        skill = self.root / PRD_GUIDE_PROPOSAL_RELATIVE
        stale = "# stale or locally edited copy\n"
        skill.write_text(stale, encoding="utf-8")
        notes = self.init().payload.get("notes", [])
        self.assertEqual(skill.read_text(encoding="utf-8"), stale)
        self.assertTrue(
            any(PRD_GUIDE_PROPOSAL_RELATIVE in note and "不一致" in note for note in notes),
            notes,
        )
        skill.write_text(load_protocol("prd-guide"), encoding="utf-8")
        quiet = self.init().payload.get("notes", [])
        self.assertFalse(any(PRD_GUIDE_PROPOSAL_RELATIVE in note for note in quiet), quiet)

    def test_complex_request_entry_reaches_a_project_that_already_has_capability_rules(self):
        """A live project already carries the capability section.

        Every gate on the AGENTS.md path used to key on the capability marker
        alone, so a project that had already applied those rules could never
        receive the PRD entry section.
        """
        from vibe_guide.scanner import CAPABILITY_RULES, PRD_GUIDE_MARKER
        agentsmd = self.root / "AGENTS.md"
        agentsmd.write_text(
            "# Existing project guidance\n\n" + CAPABILITY_RULES, encoding="utf-8"
        )
        self.init()
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        self.assertTrue(proposal.is_file(), "an incomplete AGENTS.md still needs a proposal")
        self.assertIn(PRD_GUIDE_MARKER, proposal.read_text(encoding="utf-8"))
        applied = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(applied.exit_code, 0, applied.text)
        final = agentsmd.read_text(encoding="utf-8")
        self.assertIn(PRD_GUIDE_MARKER, final)
        self.assertIn("# Existing project guidance", final)
        self.assertEqual(final.count(PRD_GUIDE_MARKER), 1, "the section must not be duplicated")
        again = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(agentsmd.read_text(encoding="utf-8"), final, again.text)


class ProposalPreservationTests(_ProjectCase):
    def test_init_never_overwrites_an_edited_agentsmd_proposal(self):
        """A proposal is awaiting human review; init must not rewrite it.

        Refreshing a stale proposal in place restored sections the reviewer had
        deliberately deleted and discarded their notes.  A pending update goes
        to a side file instead, so the reviewed bytes are never touched.
        """
        self.init()
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        text = proposal.read_text(encoding="utf-8")
        self.assertIn("## Complex Request Entry", text)
        head = text.partition("## Complex Request Entry")[0]
        edited = head.rstrip("\n") + "\n\n<!-- 这一节我们不要，已删除 -->\n"
        proposal.write_text(edited, encoding="utf-8")
        self.init()
        self.assertEqual(proposal.read_text(encoding="utf-8"), edited)

    def test_a_pending_increment_still_reaches_agentsmd(self):
        """Not rewriting the proposal must not mean dropping the increment.

        A project that applied an earlier version already has a proposal on
        disk, so the new section goes to a side file.  If nothing ever consumes
        that file the rule never reaches AGENTS.md -- a silent overwrite traded
        for a silent discard.  This walks the real upgrade: apply the old
        proposal, re-init, apply again.
        """
        from vibe_guide.scanner import PRD_GUIDE_MARKER
        agentsmd = self.root / "AGENTS.md"
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"

        # A proposal written before this release: the section did not exist, so
        # neither it nor any record of offering it is on disk.  That absence is
        # what distinguishes this from a reviewer who deleted the section.
        self._write_pre_release_proposal(proposal)
        first = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(first.exit_code, 0, first.text)
        self.assertNotIn(PRD_GUIDE_MARKER, agentsmd.read_text(encoding="utf-8"))

        self.init()
        self.assertIn("<!-- 评审备注：保留 -->", proposal.read_text(encoding="utf-8"))
        applied = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(applied.exit_code, 0, applied.text)
        final = agentsmd.read_text(encoding="utf-8")
        self.assertIn(PRD_GUIDE_MARKER, final)
        self.assertEqual(final.count(PRD_GUIDE_MARKER), 1, final)
        again = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(agentsmd.read_text(encoding="utf-8"), final, again.text)

    def test_a_section_the_reviewer_deleted_is_not_reoffered(self):
        """Reachability must not become a way around the reviewer.

        Deleting a section from the proposal is how a reviewer says no.  If
        `init` cannot tell that from "this proposal predates the section" it
        re-offers it, `apply` consumes the increment, and the deletion is
        undone -- the same outcome as rewriting the proposal in place, reached
        by a longer route.
        """
        from vibe_guide.scanner import PRD_GUIDE_MARKER
        agentsmd = self.root / "AGENTS.md"
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"

        self.init()
        text = proposal.read_text(encoding="utf-8")
        kept = text.partition("## " + PRD_GUIDE_MARKER)[0].rstrip("\n")
        proposal.write_text(kept + "\n\n<!-- 这一节我们不要 -->\n", encoding="utf-8")

        self.init()
        applied = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(applied.exit_code, 0, applied.text)
        final = agentsmd.read_text(encoding="utf-8")
        self.assertNotIn(PRD_GUIDE_MARKER, final, "the reviewer's deletion was undone")
        self.assertIn("Capability and Tool Truth", final, "the kept section must still apply")

    def test_a_rejected_increment_is_not_regenerated(self):
        """Deleting the increment file is the reviewer's answer too."""
        from vibe_guide.scanner import PRD_GUIDE_MARKER
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        increment = proposal.with_name("proposal.pending-update.md")

        self._write_pre_release_proposal(proposal)
        self.init()
        self.assertTrue(increment.is_file(), "the increment should be offered once")

        increment.unlink()
        self.init()
        self.assertFalse(increment.is_file(), "a rejected increment must not come back")

    def test_a_written_file_is_always_reported(self):
        """`changed: false` has to mean nothing on disk moved.

        The offered record is now written on every pass, so a project missing
        only that file gets a write that `changed` and `paths` both deny.  A
        caller that trusts the report -- a wrapper deciding whether to commit,
        or a reviewer reading it -- is told the tree is untouched when it is not.
        """
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        offered = proposal.with_name("proposal.offered.json")

        self._write_pre_release_proposal(proposal)
        self.init()
        offered.unlink()

        payload = run_cli(["init", "--confirm", "--json"], self.root).payload
        self.assertTrue(offered.is_file(), "the record should be restored")
        self.assertIn(
            ".vibe/proposals/agentsmd/proposal.offered.json",
            payload.get("paths", []),
            "a file was written that the report does not mention",
        )
        self.assertTrue(payload.get("changed"), payload)

    def test_the_increment_never_carries_a_section_the_proposal_already_has(self):
        """A section awaiting review must not also sit outside the proposal.

        Deciding "already in the proposal" by asking an AGENTS.md-shaped gate
        borrows its extra tokens -- `Vibe Guide` and `project` come from that
        file's title, not from a proposal -- so a proposal holding the section
        verbatim is still read as never having offered it.  The block then joins
        the increment, and deleting it from `proposal.md` no longer declines it:
        `apply` reinstates it from the side file.
        """
        from vibe_guide.scanner import CAPABILITY_RULE_MARKER, PRD_GUIDE_MARKER
        agentsmd = self.root / "AGENTS.md"
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        increment = proposal.with_name("proposal.pending-update.md")

        self._write_pre_release_proposal(proposal)
        self.init()
        headings = [
            line.strip() for line in increment.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        ]
        self.assertNotIn(
            "## " + CAPABILITY_RULE_MARKER,
            headings,
            "the proposal already holds this section; offering it twice puts it "
            "beyond the reviewer's reach",
        )

        # Deleting a section from proposal.md is how a reviewer declines it.
        proposal.write_text(
            "## " + PRD_GUIDE_MARKER + "\n\n- 只采纳这一节\n", encoding="utf-8"
        )
        self.init()
        run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertNotIn(
            CAPABILITY_RULE_MARKER,
            agentsmd.read_text(encoding="utf-8") if agentsmd.is_file() else "",
            "the declined section came back through the increment",
        )

    def test_editing_the_reviewed_proposal_does_not_wipe_the_increment(self):
        """Reviewing one section must not silently discard another.

        Whether a section is already in `proposal.md` was judged by looking for
        its whole block verbatim, while whether it had been offered was judged
        by its heading.  Under those two yardsticks a reviewer who edits a word
        of the capability section makes its block stop matching, so it becomes
        pending again and rewrites the increment -- dropping the PRD section
        along with the reviewer's own notes.  Nothing then re-offers it, because
        the offered record still lists its heading, and `notes` stays empty.
        """
        from vibe_guide.scanner import PRD_GUIDE_MARKER
        agentsmd = self.root / "AGENTS.md"
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        increment = proposal.with_name("proposal.pending-update.md")

        self._write_pre_release_proposal(proposal)
        self.init()
        self.assertIn(PRD_GUIDE_MARKER, increment.read_text(encoding="utf-8"))

        reviewed = [
            line for line in proposal.read_text(encoding="utf-8").splitlines(True)
            if not line.strip().startswith("-")
        ]
        proposal.write_text("".join(reviewed), encoding="utf-8")

        self.init()
        self.assertIn(
            PRD_GUIDE_MARKER,
            increment.read_text(encoding="utf-8") if increment.is_file() else "",
            "editing one section discarded the section still awaiting review",
        )
        applied = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(applied.exit_code, 0, applied.text)
        self.assertIn(PRD_GUIDE_MARKER, agentsmd.read_text(encoding="utf-8"))

    def test_rewriting_the_offered_record_keeps_it_readable(self):
        """The rewrite path must not re-encode what is already serialized.

        `_atomic_write` is the JSON writer, so handing it a finished string
        stores a JSON string instead of the object.  The record then parses as
        `str`, reads as "nothing offered yet", and every declined section is
        offered again -- with nothing on screen to say the file broke.
        """
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        offered = proposal.with_name("proposal.offered.json")

        self.init()
        first = json.loads(offered.read_text(encoding="utf-8"))
        self.assertIsInstance(first, dict, first)

        # Deleting the proposal sends the next init down the rewrite path.
        proposal.unlink()
        self.init()
        again = json.loads(offered.read_text(encoding="utf-8"))
        self.assertIsInstance(again, dict, again)
        self.assertIn("offered_headings", again)

    def test_an_unreadable_offered_record_is_reported_not_swallowed(self):
        """Reading it as "nothing offered" is right, but must not be silent.

        That fallback is deliberate -- withholding a new rule is worse than
        asking twice -- yet on its own it hides the damage: declined sections
        come back and the output gives no reason.  Naming the file is what makes
        "I was asked again" explainable.
        """
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        offered = proposal.with_name("proposal.offered.json")

        self.init()
        offered.write_text("not json at all", encoding="utf-8")
        result = self.init()
        self.assertIn(
            "proposal.offered.json",
            json.dumps(result.payload, ensure_ascii=False),
            result.payload,
        )

    def test_rewriting_a_pending_increment_keeps_it_markdown(self):
        """The increment is prose, so the JSON writer must not touch it."""
        from vibe_guide.scanner import PRD_GUIDE_MARKER
        proposal = self.root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
        increment = proposal.with_name("proposal.pending-update.md")

        self._write_pre_release_proposal(proposal)
        self.init()
        self.assertTrue(increment.is_file())
        # Reach the rewrite branch through init: clearing the record makes the
        # section pending again, and the differing file on disk is rewritten.
        increment.with_name("proposal.offered.json").unlink()
        increment.write_text("# 旧的增量\n\n<!-- 评审批注 -->\n", encoding="utf-8")
        self.init()
        text = increment.read_text(encoding="utf-8")
        self.assertFalse(text.startswith('"'), text[:80])
        self.assertIn("## " + PRD_GUIDE_MARKER, text)
        self.assertIn("vibe apply-agentsmd", text)

    def test_an_unclosed_fence_does_not_swallow_later_sections(self):
        """A missing closing line must not silently drop the rest.

        Tracking fences fixed the tearing, but an odd number of fence lines
        left everything after the last one inside a fence, so later headings
        stopped being boundaries and their sections vanished without a word.
        """
        from vibe_guide.initializer import _proposal_sections
        document = (
            "## Existing Section\n\n"
            "```bash\n"
            "vibe plan --print-protocol\n"
            "\n"
            "## New Rule Beta\n"
            "这一节必须仍然是一个小节。\n"
        )
        headings = [section.splitlines()[0] for section in _proposal_sections(document)]
        self.assertIn("## New Rule Beta", headings, headings)

    def test_a_tilde_fence_is_honoured_like_a_backtick_fence(self):
        """`~~~` is a fence too; ignoring it reproduces the original tearing."""
        from vibe_guide.initializer import _proposal_sections
        document = (
            "## Already Applied\n\n"
            "~~~markdown\n"
            "## Already Applied\n"
            "示例，不是真小节\n"
            "~~~\n\n"
            "这一段必须跟着它自己的小节。\n\n"
            "## Brand New\n"
            "真正要合入的内容。\n"
        )
        sections = _proposal_sections(document)
        self.assertEqual(
            [section.splitlines()[0] for section in sections],
            ["## Already Applied", "## Brand New"],
            sections,
        )
        self.assertIn("这一段必须跟着它自己的小节。", sections[0])

    def test_a_fenced_heading_is_not_read_as_a_section_boundary(self):
        """A `## ` inside ``` is example text, not a new section.

        Splitting on it tears the fence apart: the closing ``` and every line
        after it ride along with a section whose heading AGENTS.md already has,
        so they are dropped and the applied markdown has an unclosed fence.
        """
        from vibe_guide.initializer import _proposal_sections
        document = (
            "# 提案\n\n"
            "## Already Applied\n\n"
            "示例写法：\n\n"
            "```markdown\n"
            "## Already Applied\n"
            "这是示例，不是真小节\n"
            "```\n\n"
            "这一段必须跟着它自己的小节。\n\n"
            "## Brand New\n"
            "真正要合入的内容。\n"
        )
        sections = _proposal_sections(document)
        self.assertEqual(
            [section.splitlines()[0] for section in sections],
            ["## Already Applied", "## Brand New"],
            sections,
        )
        for section in sections:
            self.assertEqual(section.count("```") % 2, 0, section)
        self.assertIn("这一段必须跟着它自己的小节。", sections[0])


class ProtocolEnforcementTests(_ProjectCase):
    def test_needs_confirmation_blocks_publish(self):
        self.init()
        self.write_capabilities()
        spec = _product_spec()
        spec["prd"]["success_criteria"].append(
            {"value": "导出是否需要带公司水印尚未确认", "source": "needs_confirmation"}
        )
        result = self.plan_from_prd(spec)
        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertIn("水印", result.payload.get("question", ""))
        self.assertIn("水印", result.text)
        self.assertFalse((self.root / ".vibe" / "plans" / "pm-plan").exists())

    def test_unconfirmed_design_mockup_blocks_publish_and_confirmed_one_does_not(self):
        """§2's design-mockup gate rides on needs_confirmation, not new code.

        The protocol promises an unconfirmed mockup stops publish; that is
        only true while a free-form ``design_mockups`` section goes through
        the same checkpoint as the recommended ones.
        """
        from vibe_guide.protocols import load_protocol
        section = load_protocol("prd-guide")
        section = section[section.index("## 2."):section.index("## 3.")]
        self.assertIn("`prd.design_mockups`", section)
        self.assertIn("纯文字描述不算", section)
        self.assertIn("设计稿没确认，不得进入 §4，更不得动手写代码", section)
        self.assertIn("稿件改过，把这条改回 `needs_confirmation`", section)
        self.init()
        self.write_capabilities()
        spec = _product_spec()
        spec["prd"]["design_mockups"] = [
            {"value": "订单列表页（改动）：design/orders.png", "source": "needs_confirmation"}
        ]
        result = self.plan_from_prd(spec)
        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertIn("订单列表页", result.payload.get("question", ""))
        self.assertFalse((self.root / ".vibe" / "plans" / "pm-plan").exists())
        spec["prd"]["design_mockups"][0]["source"] = "user_confirmed"
        result = self.plan_from_prd(spec)
        self.assertNotEqual(result.payload.get("status"), "blocked_design", result.payload)

    def test_needs_confirmation_blocks_publish_without_any_other_gate_field(self):
        """The gate must not depend on unrelated keys being present.

        A spec whose only checkpoint input is the prd itself used to skip the
        checkpoint evaluation entirely and publish with items still pending.
        """
        self.init()
        self.write_capabilities()
        spec = _product_spec()
        for key in ("rationale", "product_question", "skill_profiles"):
            spec.pop(key, None)
        spec["prd"]["success_criteria"].append(
            {"value": "导出是否需要带公司水印尚未确认", "source": "needs_confirmation"}
        )
        result = self.plan_from_prd(spec)
        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertIn("水印", result.payload.get("question", ""))
        self.assertFalse((self.root / ".vibe" / "plans" / "pm-plan").exists())

    def test_a_malformed_prd_is_rejected_rather_than_silently_accepted(self):
        """A prd of the wrong shape must fail closed, not read as empty.

        The status is `blocked` rather than `blocked_design`: a wrong shape is
        a defect in what the agent wrote, not a product question waiting on
        the product manager.  What matters is that nothing publishes and the
        reason names the offending field.
        """
        self.init()
        self.write_capabilities()
        malformed_shapes = (
            ["needs_confirmation"],
            "needs_confirmation",
            7,
            {"success_criteria": 7},
            {"success_criteria": ["未包装成对象的字符串条目"]},
            {"success_criteria": [{"value": "缺少 source 标记的条目"}]},
            {"success_criteria": [{"value": "标记拼错", "source": "confirmed_by_user"}]},
        )
        for malformed in malformed_shapes:
            spec = _product_spec()
            spec["prd"] = malformed
            result = self.plan_from_prd(spec, plan_id="pm-malformed")
            self.assertEqual(result.payload.get("status"), "blocked", (malformed, result.payload))
            self.assertIn("prd", result.payload.get("reason", ""), (malformed, result.payload))
            self.assertFalse((self.root / ".vibe" / "plans" / "pm-malformed").exists())

    def test_a_confirmation_marker_with_stray_whitespace_or_case_still_blocks(self):
        """Marker comparison must not be defeated by a one-character slip."""
        self.init()
        self.write_capabilities()
        for marker in (" needs_confirmation", "needs_confirmation ", "Needs_Confirmation"):
            spec = _product_spec()
            spec["prd"]["success_criteria"].append(
                {"value": "导出是否需要带公司水印尚未确认", "source": marker}
            )
            result = self.plan_from_prd(spec, plan_id="pm-marker")
            self.assertEqual(result.payload.get("status"), "blocked_design", (marker, result.payload))
            self.assertIn("水印", result.payload.get("question", ""))
            self.assertFalse((self.root / ".vibe" / "plans" / "pm-marker").exists())

    def test_a_falsy_product_question_cannot_mask_a_pending_item(self):
        """A pending item must win over whatever the spec already put there.

        The injection used setdefault, so a spec that pre-set
        rationale.product_question to null blocked it: the checkpoint gate then
        saw no open question and published a plan with the item unconfirmed.
        """
        self.init()
        self.write_capabilities()
        for masking in (None, "", 0, False, {}):
            spec = _product_spec()
            spec["prd"]["success_criteria"].append(
                {"value": "导出是否需要带公司水印尚未确认", "source": "needs_confirmation"}
            )
            spec["rationale"] = {
                "product_question": masking,
                "framing": "verified_fact: 已确认问题框定",
                "tradeoffs": "verified_fact: 已确认取舍",
                "flow": "verified_fact: 已确认流程",
                "acceptance": "verified_fact: 已确认验收",
            }
            result = self.plan_from_prd(spec, plan_id="pm-mask")
            self.assertEqual(result.payload.get("status"), "blocked_design", (masking, result.payload))
            self.assertIn("水印", result.payload.get("question", ""), masking)
            self.assertFalse((self.root / ".vibe" / "plans" / "pm-mask").exists(), masking)

    def test_planning_brief_lists_goal_traceability_rows(self):
        self.init()
        self.write_capabilities()
        spec = _product_spec()
        spec["goals"] = [{
            "id": "G1",
            "user_scenario": "客户经理导出所选日期范围的保单 PDF",
            "code_evidence": "src/pages/policy/view.tsx",
            "spec_ref": "specs/export-button.md",
            "issue_ref": "issues/export-button.md",
            "dag_nodes": ["export-button", "date-range-filter"],
            "runtime_acceptance": "导出的 PDF 字段与页面一致",
        }]
        result = self.plan_from_prd(spec)
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        brief = (self.root / ".vibe" / "plans" / "pm-plan" / "planning-brief.md").read_text(encoding="utf-8")
        self.assertRegex(brief, r"\|\s*G1\s*\|")
        self.assertIn("export-button", brief)

    def test_incomplete_goal_blocks_publish(self):
        self.init()
        self.write_capabilities()
        spec = _product_spec()
        spec["goals"] = [{"id": "G1", "user_scenario": "只写了场景"}]
        result = self.plan_from_prd(spec)
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIn("traceability", result.payload.get("reason", ""))
        self.assertFalse((self.root / ".vibe" / "plans" / "pm-plan").exists())


if __name__ == "__main__":
    unittest.main()


class MethodologyReferenceTests(unittest.TestCase):
    """ISSUE-94: prd-guide §2 methodology reference table.

    Anchors the north-star-metric row and the rule that source marks and
    the needs_confirmation gate do not change.
    """

    def _section2(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        start = text.index("## 2. PRD")
        end = text.find("\n## 3.", start)
        return text[start:end]

    def setUp(self):
        self.section = self._section2()

    def test_lists_all_nine_skills(self):
        for name in (
            "opportunity-solution-tree",
            "north-star-metric",
            "metrics-dashboard",
            "prioritize-features",
            "prioritization-frameworks",
            "job-stories",
            "customer-journey-map",
            "strategy-red-team",
            "pre-mortem",
        ):
            self.assertIn(name, self.section, name)

    def test_north_star_metric_row_exists_in_table(self):
        rows = [l for l in self.section.splitlines() if "north-star-metric" in l]
        self.assertTrue(
            any(l.strip().startswith("|") for l in rows),
            "north-star-metric must appear as a table row",
        )

    def test_customer_journey_map_marked_optional(self):
        rows = [l for l in self.section.splitlines() if "customer-journey-map" in l]
        self.assertTrue(any("可选" in l for l in rows))

    def test_project_skill_path_shape(self):
        self.assertIn(".vibe/proposals/skills/", self.section)

    def test_source_marks_and_gate_unchanged(self):
        self.assertIn("不得改变来源标记与停等门", self.section)
        for mark in (
            "user_confirmed",
            "system_inferred",
            "needs_confirmation",
            "unverified",
        ):
            self.assertIn(mark, self.section, mark)

    def test_install_command_carries_every_argument_skill_install_requires(self):
        # cli.py rejects skill-install without --source/--sha/--name; a
        # template missing any of them fails for whoever copies it.
        commands = [l for l in self.section.splitlines() if l.startswith("vibe skill-install")]
        self.assertEqual(len(commands), 1)
        for flag in ("--source https://github.com/phuryn/pm-skills",
                     "--sha 8607e3b077817f89bf4a9b623246219734ac3be0",
                     "--subdir", "--name", "--confirm"):
            self.assertIn(flag, commands[0], flag)

    def test_decision_rules_stay_outside_methodology_subsection(self):
        head, _, _ = self.section.partition("### 2.1")
        self.assertIn("decisions[]", head)

    def test_no_markdown_side_effects(self):
        self.assertIn("另存", self.section)


class CardOptionProtocolTests(unittest.TestCase):
    """The schema block must name exactly the card tiers the code accepts.

    The PM-facing questions about heartbeat interval and rotation cap were
    retired (§0/§5.3: vibe derives them, the PM is never asked), so the old
    per-tier prose assertions are gone; the optional spec fields still exist.
    """

    def test_the_schema_block_names_exactly_the_offered_tiers(self):
        # Both directions matter: a tier added to the schema but not to the code
        # is a publish that fails after the product manager chose it, and the
        # reverse is a choice no agent can ever ask for.
        from vibe_guide.authorization import (
            HEARTBEAT_INTERVAL_CHOICES, ROTATE_HARD_CAP_CHOICES,
        )
        from vibe_guide.node_spec import PRODUCT_SPEC_FIELDS
        self.assertEqual(
            PRODUCT_SPEC_FIELDS["heartbeat_interval_minutes?"],
            "|".join(str(item) for item in HEARTBEAT_INTERVAL_CHOICES),
        )
        self.assertEqual(
            PRODUCT_SPEC_FIELDS["rotate_hard_cap?"],
            "|".join(str(item) for item in ROTATE_HARD_CAP_CHOICES),
        )


class StallSelfHealSectionTests(unittest.TestCase):
    """§6.1 must tell the supervisor what to do when a node is stalled.

    The vocabulary below is the contract between this text and the code that
    prints it (status label, review command, event type).  §6.1 now states the
    ladder as three levels (daemon retry / repair short session / escalate); the
    old tier identifiers and the preflight "优先于 `idle`" bullet were removed
    with the heartbeat runbook, so they are no longer asserted here.
    """

    STALL_LABEL = "需要监工排查"
    ESCALATE_LABEL = "需要人处理"
    REVIEW_COMMAND = "vibe heal-verify"
    TIERS = ("supervisor_investigate", "escalate_to_pm")  # code-side identifiers
    ATTEMPT_EVENT = "self_heal_attempt"
    RETIRED_LABELS = ("自动修复中", "自动推进中")

    def _levels(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("prd-guide")
        start = text.index("### 6.1 ")
        end = text.find("\n### ", start + 1)
        section = text[start:end if end != -1 else len(text)]
        items = {}
        for line in section.splitlines():
            for number in ("2", "3"):
                if line.startswith(number + ". "):
                    items[number] = line
        self.assertEqual(sorted(items), ["2", "3"], "§6.1 needs level 2 and level 3 items")
        return items["2"], items["3"], section

    def test_stall_branch_exists_in_the_heartbeat_section(self):
        investigate, escalate, _section = self._levels()
        self.assertIn(self.STALL_LABEL, investigate)
        self.assertIn(self.ESCALATE_LABEL, escalate)

    def test_stall_branch_names_the_commands_labels_and_tiers_verbatim(self):
        investigate, escalate, _section = self._levels()
        for token in (self.STALL_LABEL, self.REVIEW_COMMAND, self.ATTEMPT_EVENT):
            self.assertIn(token, investigate, token)
        self.assertIn(self.ESCALATE_LABEL, escalate)

    def test_stall_branch_orders_the_ladder_investigate_fix_verify_escalate(self):
        investigate, escalate, _section = self._levels()
        steps = ("排查包", "授权范围内修复", self.REVIEW_COMMAND)
        positions = [investigate.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions), "ladder steps out of order")
        self.assertIn("两次修复无进展", escalate)
        self.assertIn("停下报告", escalate)

    def test_stall_branch_states_the_edit_boundary(self):
        investigate, _escalate, _section = self._levels()
        self.assertIn("运行时文件", investigate)
        self.assertIn("state.json", investigate)
        self.assertIn("events.jsonl", investigate)
        self.assertIn("不得直接改", investigate)

    def test_stall_branch_keeps_the_product_and_authorization_boundary(self):
        _investigate, escalate, _section = self._levels()
        for phrase in ("产品", "授权", "deploy"):
            self.assertIn(phrase, escalate, phrase)

    def test_stall_branch_does_not_present_a_stall_as_progress(self):
        _investigate, _escalate, section = self._levels()
        for label in self.RETIRED_LABELS:
            self.assertNotIn(label, section, label)

    def test_each_tier_is_paired_with_its_own_label(self):
        investigate, escalate, _section = self._levels()
        self.assertNotIn(self.ESCALATE_LABEL, investigate)
        self.assertNotIn(self.STALL_LABEL, escalate)

    def test_readme_explains_the_stall_flow_in_user_terms(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for token in (self.STALL_LABEL, self.ESCALATE_LABEL, self.REVIEW_COMMAND):
            self.assertIn(token, readme, token)
        for label in self.RETIRED_LABELS:
            self.assertNotIn(label, readme, label)

    def test_once_code_prints_the_stall_label_it_carries_every_other_token(self):
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "vibe_guide").rglob("*.py"))
        )
        tokens = (self.STALL_LABEL, self.ESCALATE_LABEL, "heal-verify",
                  self.ATTEMPT_EVENT, *self.TIERS)
        if not any(token in sources for token in tokens):
            self.skipTest("stall wiring has not landed in vibe_guide yet")
        for token in tokens:
            self.assertIn(token, sources, token)
