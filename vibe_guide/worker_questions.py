"""A worker session that stopped to ask the supervisor something.

A background session that ends its turn (``done``/``idle``/``blocked``)
without a delivery for its generation has usually stopped to ask: widen my
file scope?  which baseline do I start from?  Reporting that as
``blocked_unknown`` hid the question in the session transcript.  Here the last
assistant message becomes an excerpt in its own file under the run directory
(the event stream redacts provider text, so the question never goes there),
the node waits in ``awaiting_supervisor``, the supervisor heartbeat raises a
``decision`` carrying the excerpt, and the supervisor's answer rides into the
next generation's prompt when ``vibe requeue --note`` re-dispatches the node.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fsio import JsonCorruptError, JsonMissingError, atomic_write_json, read_json
from .paths import ProjectPaths
from .state import AWAITING_SUPERVISOR_STATUS, load_snapshot, run_dir

WORKER_QUESTION_EVENT = "worker_question"
AWAITING_STATUS = AWAITING_SUPERVISOR_STATUS
#: Session states in which the worker is no longer working on its own.
QUESTION_STATES = frozenset({"done", "idle", "blocked"})
EXCERPT_LIMIT = 600
QUESTION_DIR = "worker-questions"
#: Node field naming the open question; structural keys only, never the text.
MARKER_KEY = "worker_question"


def excerpt(text: Any) -> Optional[str]:
    """The end of ``text`` (where the question is) in at most EXCERPT_LIMIT chars."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if len(text) <= EXCERPT_LIMIT:
        return text
    return "…" + text[-(EXCERPT_LIMIT - 1):]


def _question_name(node_id: str, role: str, generation: int) -> str:
    return "{}-{}-g{}.json".format(node_id, role, generation)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _relative(paths: ProjectPaths, path: Path) -> str:
    try:
        return path.relative_to(paths.root).as_posix()
    except ValueError:
        return path.as_posix()


def park_question(monitor: Any, snapshot: Any, node_id: str, event: Any,
                  active: Dict[str, Any]) -> None:
    """Persist the question and park the node until the supervisor answers."""
    role = str(active["role"])
    generation = int(active["generation"])
    path = run_dir(monitor.paths, snapshot.run_id, create=True) / QUESTION_DIR / _question_name(
        node_id, role, generation)
    atomic_write_json(path, {
        "run_id": snapshot.run_id,
        "node_id": node_id,
        "role": role,
        "generation": generation,
        "task_id": active.get("task_id"),
        "session_state": event.data.get("session_state"),
        "excerpt": excerpt(event.data.get("question_excerpt")),
        "asked_at": _now(),
        "answer": None,
    })
    relative = _relative(monitor.paths, path)
    # The ended session will be stopped before the node is dispatched again;
    # a terminal binding is what lets that dispatch continue the same task.
    monitor._set_binding_status(snapshot, node_id, role, "stopped")
    current = snapshot.nodes[node_id]
    current["status"] = AWAITING_STATUS
    current["reason"] = "worker session ended without delivery and asked the supervisor"
    current[MARKER_KEY] = {"file": relative, "role": role, "generation": generation}
    current["active_role"] = None
    current["active_task"] = None
    current["quarantine"] = None
    current["retryable_action"] = None
    snapshot.handles.pop(node_id, None)
    monitor._release_node_lease(snapshot, node_id)
    monitor._record(snapshot, "worker_question_raised", {
        "run_id": snapshot.run_id,
        "node_id": node_id,
        "question_file": relative,
        "session_state": event.data.get("session_state"),
    }, active)


def pending_questions(paths: ProjectPaths, snapshot: Any) -> List[Dict[str, Any]]:
    """Open questions of the run's parked nodes, excerpt included."""
    found = []
    for node_id, current in sorted((getattr(snapshot, "nodes", None) or {}).items()):
        if not isinstance(current, dict) or current.get("status") != AWAITING_STATUS:
            continue
        marker = current.get(MARKER_KEY) if isinstance(current.get(MARKER_KEY), dict) else {}
        item = {"node_id": node_id, "role": marker.get("role"),
                "generation": marker.get("generation"), "file": marker.get("file"),
                "excerpt": None}
        try:
            record = read_json(paths.root / str(marker.get("file")))
        except (JsonMissingError, JsonCorruptError, TypeError):
            record = None
        if isinstance(record, dict):
            item["excerpt"] = record.get("excerpt")
            item["session_state"] = record.get("session_state")
        else:
            item["excerpt"] = "（问题文件不可读，请直接查看 worker 会话记录）"
        found.append(item)
    return found


def answer_question(monitor: Any, run_id: str, node_id: str, answer: Any) -> Dict[str, Any]:
    """Record the supervisor's answer and queue the same node for re-dispatch."""
    text = answer.strip() if isinstance(answer, str) else ""
    if not text:
        raise ValueError("答复内容为空：用 --note 写明给 worker 的答复")
    snapshot = load_snapshot(monitor.paths, run_id)
    monitor._require_snapshot_authorization(snapshot)
    current = snapshot.nodes.get(node_id)
    marker = current.get(MARKER_KEY) if isinstance(current, dict) else None
    if not isinstance(current, dict) or current.get("status") != AWAITING_STATUS \
            or not isinstance(marker, dict):
        raise ValueError("节点 {} 没有待监工答复的 worker 问题".format(node_id))
    path = monitor.paths.root / str(marker.get("file"))
    record = read_json(path)
    if not isinstance(record, dict):
        raise ValueError("worker 问题文件格式不对：{}".format(marker.get("file")))
    record["answer"] = text
    record["answered_at"] = _now()
    atomic_write_json(path, record)
    role = str(marker.get("role") or "developer")
    current["status"] = "rework"
    current["reason"] = None
    current["pair_archived"] = True
    current["retryable_action"] = {
        "role": role,
        "phase": "review" if role == "reviewer" else "rework",
        "continuation": True,
        "pending_schedule": True,
        "successor": False,
        "successor_candidate": False,
    }
    current[MARKER_KEY] = dict(marker, answered=True)
    monitor._record(snapshot, "worker_question_answered", {
        "run_id": run_id, "node_id": node_id, "question_file": marker.get("file"),
    })
    monitor._save_snapshot(snapshot)
    return {"node_id": node_id, "status": current["status"], "file": marker.get("file"),
            "role": role, "run_id": run_id}


def supervisor_answer_section(contract: Dict[str, Any]) -> str:
    """The answered question of the previous generation, for the next prompt."""
    try:
        generation = int(contract.get("generation"))
        root = Path(str(contract["project_root"]))
        path = run_dir(ProjectPaths(root), str(contract["run_id"])) / QUESTION_DIR / _question_name(
            str(contract["node_id"]), str(contract.get("role") or "developer"), generation - 1)
        record = read_json(path)
    except (KeyError, TypeError, ValueError, OSError):
        return ""
    answer = record.get("answer") if isinstance(record, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        return ""
    return (
        "上一代你停下来向监工提问：「{}」\n监工答复：{}\n"
        "按监工答复继续本节点工作，完成后照常交付。\n"
    ).format(record.get("excerpt") or "", answer.strip())


def render_beat_questions(questions: List[Dict[str, Any]], run_id: str, plan_id: str,
                          command_prefix: str = "vibe") -> str:
    lines = ["decision：worker 停下来向监工提问，由监工判断并答复（只有产品取舍才转问产品经理）："]
    for item in questions:
        lines.append("  节点 {}（{} 第 {} 代）：「{}」 全文：{}".format(
            item["node_id"], item.get("role"), item.get("generation"),
            item.get("excerpt") or "", item.get("file")))
        lines.append("    答复：{} requeue --plan {} --run-id {} --node {} --note '<答复>'".format(
            command_prefix, plan_id, run_id, item["node_id"]))
    lines.append("答复后执行 {} resume --plan {} --run-id {} 续派同一节点".format(
        command_prefix, plan_id, run_id))
    return "\n".join(lines)


def render_status_lines(paths: Optional[ProjectPaths], snapshot: Any) -> str:
    if paths is None:
        return ""
    questions = pending_questions(paths, snapshot)
    if not questions:
        return ""
    return "\n".join(
        "需要监工答复：节点 {} 的 worker 提问「{}」（全文 {}）".format(
            item["node_id"], item.get("excerpt") or "", item.get("file"))
        for item in questions)
