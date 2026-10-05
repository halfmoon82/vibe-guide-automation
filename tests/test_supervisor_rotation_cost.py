"""Supervisor rotation must save tokens, not spend them.

Observed on run 140 (2026-10-05, vibe 5.0.4 on Codex, 13 shifts in 10.5h):

- 46 of 60 preflights said ``work`` because one dispatch was held for a
  human A/B decision; nothing on disk said so, so the idle fast path never
  ran and every beat cost 3-5 model calls and grew the context.
- The 60k threshold was absolute while a fresh shift already sat at 44-70k
  after reading its handoff, so late shifts rotated 2-7 minutes after taking
  over -- each takeover cost 0.4-1.7M input tokens.
- The shift wrote ``{"token_count": N}`` by hand every beat, after the
  preflight, so the check always saw the previous beat.
- A takeover re-read several prd-guide sections, the authorization card and
  the full status (11-31 calls).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import (
    DEFAULT_ROTATE_HARD_CAP,
    heartbeat_prompt,
    register_supervisor_address,
    release_supervisor_hold,
    set_supervisor_hold,
    supervisor_holds,
    supervisor_preflight,
)

from tests.support_v45_authorize import publish_complex_probe


def _record(directory, tokens, name="session.json"):
    path = Path(directory) / name
    path.write_text(json.dumps({"token_count": tokens}), encoding="utf-8")
    return str(path)


def _codex_rollout(directory, contexts, name="rollout.jsonl"):
    """A Codex Desktop session log: token_count events carry the context."""
    lines = [{"type": "session_meta", "payload": {"id": "s"}}]
    lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": None}})
    for context in contexts:
        lines.append({"type": "event_msg", "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": 10 ** 7},
                "last_token_usage": {
                    "input_tokens": context, "cached_input_tokens": context - 100,
                },
            },
        }})
        lines.append({"type": "response_item", "payload": {"type": "message"}})
    path = Path(directory) / name
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + '\n{"partial',
        encoding="utf-8",
    )
    return str(path)


class _FakeSnapshot:
    def __init__(self, statuses, ready_set=()):
        self.nodes = {node: {"status": status} for node, status in statuses.items()}
        self.ready_set = list(ready_set)


def _patch_snapshot(statuses, ready_set=()):
    return patch(
        "vibe_guide.supervisor.load_snapshot",
        return_value=_FakeSnapshot(statuses, ready_set),
    )


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)

    def register(self, session_record=None):
        return register_supervisor_address(
            self.paths, "run-1",
            {"provider": "codex", "session_id": "s-1", "host": "mac"},
            session_record=session_record,
        )

    def pending(self, node):
        directory = self.paths.vibe / "provider-actions" / "requests"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "action-{}.json".format(node)).write_text(json.dumps({
            "schema_version": 1, "action_id": "action-" + node,
            "operation": "create", "provider": "codex",
            "run_id": "run-1", "issue_id": node, "role": "reviewer",
            "generation": 1, "sequence": 0,
            "native_tool": "codex_app__create_thread",
            "request": {"prompt": "x"}, "request_digest": "0" * 64,
        }), encoding="utf-8")


class CodexSessionLogTests(_Case):
    """Fix 3: the preflight reads the shift's own log; no hand-written record."""

    def test_reads_the_latest_context_from_a_codex_rollout(self):
        path = _codex_rollout(self.tmp.name, [30000, 52000, 71000])
        result = supervisor_preflight(self.paths, "run-1", path)
        self.assertEqual(result["state"], "rotate")
        self.assertEqual(result["tokens"], 71000)

    def test_cumulative_totals_are_not_mistaken_for_context(self):
        path = _codex_rollout(self.tmp.name, [30000, 41000])
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(self.paths, "run-1", path)
        self.assertEqual(result["state"], "idle", result)

    def test_rollout_with_no_usage_yet_is_unknown(self):
        path = _codex_rollout(self.tmp.name, [])
        result = supervisor_preflight(self.paths, "run-1", path)
        self.assertEqual(result["state"], "unknown")

    def test_hand_written_record_still_works(self):
        """Shifts installed before this change keep their old heartbeat."""
        result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 70000))
        self.assertEqual(result["state"], "rotate")


class RelativeThresholdTests(_Case):
    """Fix 2: rotate on growth since takeover, never right after taking over."""

    def test_takeover_floor_above_the_old_threshold_does_not_rotate(self):
        # Shift 10 on run 140 sat at 70k right after its handoff.
        path = _codex_rollout(self.tmp.name, [30000, 70000])
        self.register(session_record=path)
        _codex_rollout(self.tmp.name, [30000, 70000, 72000])
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(self.paths, "run-1", path)
        self.assertEqual(result["state"], "idle", result)
        self.assertEqual(result["baseline"], 70000)

    def test_rotates_once_growth_since_takeover_passes_the_threshold(self):
        path = _codex_rollout(self.tmp.name, [30000, 50000])
        self.register(session_record=path)
        _codex_rollout(self.tmp.name, [30000, 50000, 110001])
        result = supervisor_preflight(self.paths, "run-1", path)
        self.assertEqual(result["state"], "rotate")
        self.assertEqual(result["reason"], "context grew past threshold since takeover")

    def test_growth_threshold_is_configurable(self):
        path = _codex_rollout(self.tmp.name, [50000])
        self.register(session_record=path)
        _codex_rollout(self.tmp.name, [50000, 80000])
        result = supervisor_preflight(self.paths, "run-1", path, token_threshold=20000)
        self.assertEqual(result["state"], "rotate")

    def test_hard_cap_rotates_whatever_the_baseline(self):
        path = _codex_rollout(self.tmp.name, [DEFAULT_ROTATE_HARD_CAP - 10000])
        self.register(session_record=path)
        _codex_rollout(self.tmp.name, [DEFAULT_ROTATE_HARD_CAP + 1])
        result = supervisor_preflight(self.paths, "run-1", path)
        self.assertEqual(result["state"], "rotate")
        self.assertEqual(result["reason"], "context over hard cap")

    def test_configured_threshold_above_the_cap_lifts_the_cap(self):
        rec = _record(self.tmp.name, DEFAULT_ROTATE_HARD_CAP + 1)
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(
                self.paths, "run-1", rec, token_threshold=DEFAULT_ROTATE_HARD_CAP * 2,
            )
        self.assertEqual(result["state"], "idle", result)

    def test_baseline_belongs_to_the_registered_log_only(self):
        """A different shift's log must not borrow this shift's baseline."""
        mine = _codex_rollout(self.tmp.name, [65000], name="mine.jsonl")
        self.register(session_record=mine)
        other = _codex_rollout(self.tmp.name, [70000], name="other.jsonl")
        result = supervisor_preflight(self.paths, "run-1", other)
        self.assertEqual(result["state"], "rotate")
        self.assertIsNone(result["baseline"])

    def test_registration_without_a_log_keeps_the_absolute_threshold(self):
        self.register()
        result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 70000))
        self.assertEqual(result["state"], "rotate")

    def test_registering_the_same_shift_again_keeps_its_baseline(self):
        path = _codex_rollout(self.tmp.name, [45000])
        self.register(session_record=path)
        _codex_rollout(self.tmp.name, [45000, 90000])
        entry = self.register(session_record=path)
        self.assertEqual(entry["baseline_context"], 45000)

    def test_relative_log_path_records_no_baseline(self):
        """Review P3: a relative path resolves differently per working directory."""
        import os

        _codex_rollout(self.tmp.name, [45000])
        cwd = os.getcwd()
        os.chdir(self.tmp.name)  # the relative path really is readable here
        self.addCleanup(os.chdir, cwd)
        entry = self.register(session_record="rollout.jsonl")
        self.assertNotIn("baseline_context", entry)

    def test_registry_keeps_no_path_to_the_log(self):
        path = _codex_rollout(self.tmp.name, [40000])
        self.register(session_record=path)
        text = (self.paths.vibe / "runs" / "run-1" / "supervisor-registry.json").read_text("utf-8")
        self.assertNotIn(self.tmp.name, text)
        self.assertIn('"baseline_context": 40000', text)


class HoldTests(_Case):
    """Fix 1: a node waiting on a human is idle, and the disk says so."""

    def test_pending_request_of_a_held_node_is_idle(self):
        self.pending("integration-review")
        statuses = {"a": "accepted", "integration-review": "ready"}
        with _patch_snapshot(statuses):
            set_supervisor_hold(self.paths, "run-1", "integration-review", "等用户拍板 A/B")
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "idle", result)
        self.assertEqual(result["held"], ["integration-review"])

    def test_unheld_pending_request_is_still_work(self):
        self.pending("integration-review")
        self.pending("n2")
        statuses = {"integration-review": "ready", "n2": "running"}
        with _patch_snapshot(statuses):
            set_supervisor_hold(self.paths, "run-1", "integration-review", "等用户拍板")
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "work")
        self.assertEqual(result["pending"], 1)

    def test_other_nodes_needing_service_are_still_work(self):
        statuses = {"integration-review": "ready", "n2": "retry_pending"}
        with _patch_snapshot(statuses):
            set_supervisor_hold(self.paths, "run-1", "integration-review", "等用户拍板")
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "work")
        self.assertEqual(result["nodes"], ["n2"])

    def test_a_delivery_on_a_held_node_still_wakes_the_supervisor(self):
        with _patch_snapshot({"integration-review": "ready"}):
            set_supervisor_hold(self.paths, "run-1", "integration-review", "等用户拍板")
            with patch(
                "vibe_guide.adapters.task_provider.ProviderActionStore.unconsumed_deliveries",
                return_value=["integration-review"],
            ):
                result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "work")

    def test_release_restores_work(self):
        self.pending("integration-review")
        with _patch_snapshot({"integration-review": "ready"}):
            set_supervisor_hold(self.paths, "run-1", "integration-review", "等用户拍板")
            self.assertTrue(release_supervisor_hold(self.paths, "run-1", "integration-review"))
            self.assertFalse(release_supervisor_hold(self.paths, "run-1", "integration-review"))
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "work")
        self.assertEqual(supervisor_holds(self.paths, "run-1"), {})

    def test_unreadable_hold_file_is_unknown_not_idle(self):
        directory = self.paths.vibe / "runs" / "run-1"
        directory.mkdir(parents=True)
        (directory / "supervisor-holds.json").write_text("{broken", encoding="utf-8")
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "unknown")

    def test_held_middle_node_with_planned_downstream_is_idle(self):
        """Review P1: the held node is often not the DAG's last node."""
        statuses = {"a": "accepted", "x": "ready", "y": "planned"}
        with _patch_snapshot(statuses):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "idle", result)
        self.assertEqual(result["held"], ["x"])

    def test_hold_does_not_hide_an_independent_dispatchable_node(self):
        """Review round 2 P2: Z depends only on accepted A, so it can start now."""
        statuses = {"a": "accepted", "z": "planned", "x": "ready"}
        with _patch_snapshot(statuses, ready_set=["x", "z"]):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "work", result)
        self.assertEqual(result["nodes"], ["z"])

    def test_queued_nodes_behind_full_capacity_stay_idle_without_holds(self):
        """Review round 3 P1: ready_set ignores capacity; a full run is still idle."""
        statuses = {"n1": "running", "n2": "planned", "n3": "planned"}
        with _patch_snapshot(statuses, ready_set=["n2", "n3"]):
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "idle", result)
        self.assertEqual(result["reason"], "workers active")

    def test_queued_nodes_behind_full_capacity_stay_idle_with_a_hold(self):
        statuses = {"x": "ready", "n1": "running", "n2": "planned"}
        with _patch_snapshot(statuses, ready_set=["x", "n2"]):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "idle", result)
        self.assertEqual(result["held"], ["x"])

    def test_hold_from_the_future_is_rechecked(self):
        """Review round 2 P3: a clock moved back must not extend a hold."""
        with _patch_snapshot({"x": "ready"}):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")
            earlier = __import__("time").time() - 3600
            with patch("vibe_guide.supervisor.time.time", return_value=earlier):
                result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["reason"], "hold needs recheck")

    def test_re_holding_one_node_does_not_revive_another(self):
        """Review round 2 P3: each voided hold needs its own recheck."""
        with _patch_snapshot({"x": "ready", "y": "ready"}):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板 x")
            set_supervisor_hold(self.paths, "run-1", "y", "等用户拍板 y")
        changed = {"x": "blocked_unknown", "y": "ready"}
        with _patch_snapshot(changed):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板 x")
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "work", result)
        self.assertEqual(result["nodes"], ["y"])

    def test_any_status_change_since_the_hold_voids_it(self):
        """Review P2: a hold must not hide what happened after it."""
        with _patch_snapshot({"x": "ready", "y": "planned"}):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")
        for changed in ({"x": "blocked_unknown", "y": "planned"},
                        {"x": "ready", "y": "ready"},
                        {"x": "ready", "y": "planned", "z": "planned"}):
            with self.subTest(changed=changed), _patch_snapshot(changed):
                result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
                self.assertEqual(result["state"], "work", result)
                self.assertEqual(result["reason"], "hold needs recheck")

    def test_holding_again_refreshes_to_the_current_state(self):
        with _patch_snapshot({"x": "ready", "y": "planned"}):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")
        with _patch_snapshot({"x": "blocked_unknown", "y": "planned"}):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")
            result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "idle", result)

    def test_old_hold_wakes_the_supervisor_to_recheck(self):
        """Review P2: a hold nobody released must not stall the run silently."""
        from vibe_guide.supervisor import HOLD_RECHECK_SECONDS

        with _patch_snapshot({"x": "ready"}):
            set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")
            later = __import__("time").time() + HOLD_RECHECK_SECONDS + 1
            with patch("vibe_guide.supervisor.time.time", return_value=later):
                result = supervisor_preflight(self.paths, "run-1", _record(self.tmp.name, 30000))
        self.assertEqual(result["state"], "work")
        self.assertEqual(result["reason"], "hold needs recheck")

    def test_review_and_rework_cannot_be_held(self):
        for status in ("review", "rework"):
            with self.subTest(status=status), _patch_snapshot({"x": status}):
                with self.assertRaises(ValueError):
                    set_supervisor_hold(self.paths, "run-1", "x", "等用户拍板")

    def test_preflight_on_an_unknown_run_creates_nothing(self):
        """Review P3: the preflight promises a read-only disk check."""
        supervisor_preflight(self.paths, "run-typo", _record(self.tmp.name, 30000))
        self.assertFalse((self.paths.vibe / "runs" / "run-typo").exists())

    def test_hold_needs_a_known_node_and_a_reason(self):
        with _patch_snapshot({"n1": "ready"}):
            with self.assertRaises(ValueError):
                set_supervisor_hold(self.paths, "run-1", "typo", "等用户拍板")
            with self.assertRaises(ValueError):
                set_supervisor_hold(self.paths, "run-1", "n1", "  ")
            with self.assertRaises(ValueError):
                set_supervisor_hold(self.paths, "run-1", "n1", "x" * 201)


class CliTests(unittest.TestCase):
    def start(self):
        root = publish_complex_probe(self)
        run_cli(["authorize", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"], root)
        started = run_cli(["monitor", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"], root)
        run_id = started.payload["run_id"]
        node = sorted(started.payload.get("nodes") or ["?"])[0]
        return root, run_id, node

    def test_hold_and_release_roundtrip(self):
        root, run_id, node = self.start()
        held = run_cli([
            "supervisor-hold", "--json", "--run-id", run_id,
            "--node", node, "--reason", "等用户拍板 A/B",
        ], root)
        self.assertEqual(held.exit_code, 0, held.payload)
        self.assertEqual(held.payload["holds"][node]["reason"], "等用户拍板 A/B")
        released = run_cli([
            "supervisor-hold", "--json", "--run-id", run_id, "--node", node, "--release",
        ], root)
        self.assertEqual(released.exit_code, 0, released.payload)
        self.assertEqual(released.payload["holds"], {})

    def test_hold_on_unknown_node_is_blocked(self):
        root, run_id, _node = self.start()
        result = run_cli([
            "supervisor-hold", "--json", "--run-id", run_id,
            "--node", "no-such-node", "--reason", "x",
        ], root)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "blocked_invalid")

    def test_register_records_the_takeover_baseline(self):
        root, run_id, _node = self.start()
        log = _codex_rollout(root, [30000, 47000])
        result = run_cli([
            "supervisor-register", "--json", "--run-id", run_id, "--provider", "codex",
            "--session-id", "s-1", "--host", "mac", "--session-record", log,
        ], root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["current"]["baseline_context"], 47000)

    def test_register_with_unreadable_log_still_registers_but_says_so(self):
        root, run_id, _node = self.start()
        result = run_cli([
            "supervisor-register", "--run-id", run_id, "--provider", "codex",
            "--session-id", "s-1", "--host", "mac", "--session-record", "/nonexistent.jsonl",
        ], root)
        self.assertEqual(result.exit_code, 0, result.text)
        self.assertNotIn("baseline_context", result.payload["current"])
        self.assertIn("接班起点", result.text)

    def test_handoff_prints_everything_a_new_shift_needs(self):
        root, run_id, node = self.start()
        run_cli([
            "supervisor-hold", "--run-id", run_id, "--node", node, "--reason", "等用户拍板 A/B",
        ], root)
        result = run_cli(["supervisor-handoff", "--run-id", run_id], root)
        self.assertEqual(result.exit_code, 0, result.text)
        self.assertIn(heartbeat_prompt("probe-plan", run_id), result.text)
        self.assertIn("等用户拍板 A/B", result.text)
        self.assertIn("--session-record <本会话记录路径>", result.text)
        self.assertIn("vibe supervisor-register --run-id " + run_id, result.text)
        as_json = run_cli(["supervisor-handoff", "--json", "--run-id", run_id], root)
        self.assertEqual(as_json.payload["plan_id"], "probe-plan")
        self.assertEqual(list(as_json.payload["holds"]), [node])
        self.assertIn(node, as_json.payload["nodes"])

    def test_hold_on_a_real_run_turns_the_preflight_idle(self):
        """Review P3: the hold-to-idle chain on a real snapshot, no mocks."""
        root, run_id, _node = self.start()
        log = _codex_rollout(root, [30000])
        before = run_cli([
            "supervisor-preflight", "--json", "--run-id", run_id, "--session-record", log,
        ], root)
        self.assertEqual(before.payload["state"], "work", before.payload)
        node = before.payload["nodes"][0]
        run_cli([
            "supervisor-hold", "--run-id", run_id, "--node", node, "--reason", "等用户拍板",
        ], root)
        after = run_cli([
            "supervisor-preflight", "--json", "--run-id", run_id, "--session-record", log,
        ], root)
        self.assertEqual(after.payload["state"], "idle", after.payload)
        self.assertEqual(after.payload["held"], [node])

    def test_handoff_with_invalid_run_id_is_blocked(self):
        root, _run_id, _node = self.start()
        result = run_cli(["supervisor-handoff", "--json", "--run-id", "../x"], root)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "blocked_invalid")

    def test_handoff_for_unknown_run_is_unknown(self):
        root, _run_id, _node = self.start()
        result = run_cli(["supervisor-handoff", "--json", "--run-id", "run-missing"], root)
        self.assertNotEqual(result.exit_code, 0)


class HeartbeatRotateBranchTests(unittest.TestCase):
    """Fix 4: the rotate branch hands over through vibe, not through prd-guide."""

    def test_rotate_branch_runs_the_handoff_command(self):
        text = heartbeat_prompt("plan-a", "run-b")
        rotate = [line for line in text.splitlines() if line.strip().startswith("- rotate")]
        self.assertEqual(len(rotate), 1)
        self.assertIn("vibe supervisor-handoff --run-id run-b", rotate[0])


if __name__ == "__main__":
    unittest.main()
