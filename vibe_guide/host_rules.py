"""User-level alignment-gate exception for vibe supervisor sessions (#152).

``vibe init`` asks once whether the user-level rule files should carry a
marker-bounded snippet that lets *supervisor* sessions handle heartbeat and
takeover turns in light mode. Writing outside the project is a sensitive,
external action, so the flow is: preview diff + digest -> explicit
authorization by digest -> verified backup -> atomic replace; rollback
restores the backup (or removes only the block when the user edited the file
elsewhere afterwards). Existing user files are never created, followed
through symlinks or rewritten when the block was hand-edited.
"""

import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path


HOST_RULES_ID = "host_supervisor_rules"
SNIPPET_VERSION = 1
#: User-level rule files, relative to home, in the order hosts read them.
CANDIDATES = (".claude/CLAUDE.md", ".codex/AGENTS.md", "AGENTS.md")
BACKUP_SUFFIX = ".vibe-host-rules.bak"
RECORD_RELATIVE = Path(".vibe") / "host-rules.json"
BEGIN_PREFIX = "<!-- vibe-guide:host-supervisor-rules begin"
END_MARKER = "<!-- vibe-guide:host-supervisor-rules end -->"
QUESTION = "是否在用户级规则中加入 vibe 监工会话例外？"
DECISIONS = ("skip", "apply", "rolled_back")

_BEGIN_LINE = re.compile(
    r"<!-- vibe-guide:host-supervisor-rules begin v(\d+) sha256=([0-9a-f]{64}) -->\n"
)

SNIPPET_BODY = (
    "## vibe 监工会话例外\n"
    "\n"
    "- 本例外只作用于带 vibe 监工会话标记的会话。标记须同时满足："
    "① 本会话已通过 `vibe supervisor-register` 登记为某个 run 的监工；"
    "② 该 run 的授权卡已签；"
    "③ 当前回合由 `vibe supervisor-beat --run-id <run-id>` 心跳或 "
    "`vibe supervisor-handoff` 接班指令发起。\n"
    "- 满足标记的心跳与接班回合按 light 模式处理，不输出对齐门卡片。\n"
    "- 只有 vibe 边界判定（PRD §5.7：product_acceptance / external_authorization）"
    "认定需要用户的事项才进入隐藏区并停下等用户回答。\n"
    "- 纯工程问题（报错、权限措辞、严重度未知等）由监工按 vibe 协议自行处理，"
    "不作为隐藏区卡片上浮。\n"
    "- 其他会话、其他项目的对齐门行为保持不变。\n"
    "- 回滚：在登记该例外的项目里运行 `vibe init --confirm --host-rules rollback`。\n"
)


class HostRulesError(Exception):
    """A refusal with a user-facing (Chinese) reason; nothing was written."""


def _sha(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _render_block(body, version):
    return (
        BEGIN_PREFIX + " v" + str(version) + " sha256=" + _sha(body) + " -->\n"
        + body + END_MARKER
    )


CURRENT_BLOCK = _render_block(SNIPPET_BODY, SNIPPET_VERSION)


def _label(rel):
    return "~/" + rel


def _check_path(home, rel):
    """Return the absolute path for *rel*; refuse symlinks anywhere below home.

    A symlinked rule file or directory (dotfiles setups) would make the write
    and the backup land outside the place the user previewed.
    """
    path = Path(home) / rel
    current = Path(home)
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            raise HostRulesError(_label(rel) + "：路径含符号链接，拒绝写入")
    return path


def _find_block(text):
    """Locate the block: None, or (start, end, version, intact).

    Raises when markers are duplicated or unbalanced, because then no single
    span can be replaced without guessing.
    """
    begins = text.count(BEGIN_PREFIX)
    ends = text.count(END_MARKER)
    if begins == 0 and ends == 0:
        return None
    start = text.find(BEGIN_PREFIX)
    end_at = text.find(END_MARKER)
    if begins != 1 or ends != 1 or end_at < start:
        raise HostRulesError("标记块不完整或重复，标记块被人工改过")
    end = end_at + len(END_MARKER)
    match = _BEGIN_LINE.match(text, start)
    if match is None or match.end() > end_at:
        return start, end, None, False
    body = text[match.end():end_at]
    return start, end, int(match.group(1)), _sha(body) == match.group(2)


def _read_target(path, rel):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise HostRulesError(_label(rel) + "：不是普通文件，拒绝写入")
    data = path.read_bytes()
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        raise HostRulesError(_label(rel) + "：不是 UTF-8 文本，拒绝写入") from None


def _propose(text, rel):
    found = _find_block(text)
    if found is None:
        separator = "" if text.endswith("\n") or not text else "\n"
        lead = "\n" if text else ""
        return text + separator + lead + CURRENT_BLOCK + "\n", "append"
    start, end, _version, intact = found
    if not intact:
        raise HostRulesError(_label(rel) + "：vibe 监工会话例外标记块被人工改过，拒绝覆盖")
    if text[start:end] == CURRENT_BLOCK:
        return text, "current"
    return text[:start] + CURRENT_BLOCK + text[end:], "upgrade"


def _digest(entries):
    canonical = json.dumps(
        {"version": SNIPPET_VERSION, "targets": entries},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return _sha(canonical)


def plan_apply(home):
    """Preview the change for every existing candidate; never writes.

    ``digest`` is None whenever anything is refused or there is nothing to
    target, so an authorization can only ever cover a clean, complete plan.
    """
    targets, errors = [], []
    for rel in CANDIDATES:
        try:
            path = _check_path(home, rel)
            read = _read_target(path, rel)
            if read is None:
                continue
            data, text = read
            proposed, action = _propose(text, rel)
        except HostRulesError as error:
            errors.append(str(error))
            continue
        except OSError as error:
            errors.append(_label(rel) + "：读取失败（" + error.__class__.__name__ + "）")
            continue
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True), proposed.splitlines(keepends=True),
            fromfile=_label(rel), tofile=_label(rel),
        ))
        targets.append({
            "rel": rel, "path": _label(rel), "action": action,
            "current": data, "proposed": proposed.encode("utf-8"), "diff": diff,
        })
    if not targets and not errors:
        errors.append(
            "未找到用户级规则文件（" + "、".join(_label(r) for r in CANDIDATES)
            + "），不会新建"
        )
    digest = None
    if not errors:
        digest = _digest([[t["rel"], _sha(t["current"]), _sha(t["proposed"])] for t in targets])
    return {
        "targets": targets, "errors": errors, "digest": digest,
        "diff": "".join(t["diff"] for t in targets),
    }


def _atomic_write_bytes(path, data, mode):
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _backup_path(path):
    return Path(str(path) + BACKUP_SUFFIX)


def _regular_bytes(path):
    """Bytes of *path* when it is a regular file (not a symlink), else None."""
    try:
        if stat.S_ISREG(os.lstat(path).st_mode):
            return path.read_bytes()
    except FileNotFoundError:
        return None
    return None


def _write_backup(path, original, rel):
    """Write the bytes this apply is about to replace, then verify them.

    The backup is always rewritten: a stale one left by an older apply (or by
    another project) must never be what a later rollback restores. Returns
    the previous backup bytes (or None) so a failed apply can put them back.
    """
    backup = _backup_path(path)
    if os.path.lexists(backup) and (backup.is_symlink() or not backup.is_file()):
        raise HostRulesError(_label(rel) + "：备份位置已被非普通文件占用，备份失败")
    previous = _regular_bytes(backup)
    try:
        _atomic_write_bytes(backup, original, 0o600)
        if backup.read_bytes() != original:
            raise OSError("backup verification mismatch")
    except OSError as error:
        _restore_backup(backup, previous)
        raise HostRulesError(
            _label(rel) + "：备份失败（" + error.__class__.__name__ + "）"
        ) from None
    return previous


def _restore_backup(backup, previous):
    try:
        if previous is None:
            if backup.is_file() and not backup.is_symlink():
                backup.unlink()
        else:
            _atomic_write_bytes(backup, previous, 0o600)
    except OSError:
        pass


def _entry(rel, written, backed_up):
    """Restore-capable entry: rollback may restore the backup only while both
    the file and the backup still hold exactly these bytes."""
    path_label = _label(rel)
    return {
        "path": path_label,
        "backup": path_label + BACKUP_SUFFIX,
        "written_sha256": _sha(written),
        "backup_sha256": _sha(backed_up),
    }


def _valid_entry(entry, label):
    if not isinstance(entry, dict) or entry.get("path") != label:
        return False
    if entry.get("restore") == "block":
        return True
    return all(
        isinstance(entry.get(key), str) and re.fullmatch("[0-9a-f]{64}", entry[key])
        for key in ("written_sha256", "backup_sha256")
    )


def _current_entry(label, previous):
    """Entry for a file this apply did not write.

    Nothing was backed up now, so a fresh restore-capable entry would let a
    rollback restore someone else's (or an outdated) backup over later edits.
    Keep this project's earlier entry when it is well-formed (rollback
    re-checks its hashes); otherwise rollback may only remove the block.
    """
    for entry in previous or ():
        if _valid_entry(entry, label):
            return entry
    return {"path": label, "restore": "block"}


def apply(home, digest, previous=None):
    """Write the snippet only when *digest* matches a fresh, clean plan.

    *previous* is this project's recorded entry list, reused for files that
    are already current.
    """
    plan = plan_apply(home)
    if plan["errors"]:
        return {"status": "blocked", "reason": "；".join(plan["errors"]), "entries": [], "paths": []}
    if not digest or digest != plan["digest"]:
        return {
            "status": "blocked",
            "reason": "授权摘要与当前差异不一致（已过期或未授权），未写入",
            "entries": [], "paths": [],
        }
    changes = [t for t in plan["targets"] if t["action"] != "current"]
    entries = [
        _entry(t["rel"], t["proposed"], t["current"]) if t["action"] != "current"
        else _current_entry(t["path"], previous)
        for t in plan["targets"]
    ]
    if not changes:
        return {"status": "current", "entries": entries, "paths": []}
    home = Path(home)
    for target in changes:
        path = home / target["rel"]
        if not (os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)):
            return {
                "status": "blocked",
                "reason": target["path"] + "：文件或所在目录不可写，未写入任何文件",
                "entries": [], "paths": [],
            }
    saved = []
    try:
        for target in changes:
            backup = _backup_path(home / target["rel"])
            saved.append((backup, _write_backup(home / target["rel"], target["current"], target["rel"])))
    except HostRulesError as error:
        for backup, previous_bytes in saved:
            _restore_backup(backup, previous_bytes)
        return {"status": "blocked", "reason": str(error) + "，未写入任何文件", "entries": [], "paths": []}
    written = []
    try:
        for target in changes:
            path = home / target["rel"]
            # The plan was read moments ago; refuse if the file moved under us.
            if path.read_bytes() != target["current"]:
                raise HostRulesError(target["path"] + "：文件在预览后被改动")
            _atomic_write_bytes(path, target["proposed"], stat.S_IMODE(os.lstat(path).st_mode))
            written.append(target)
    except (HostRulesError, OSError) as error:
        for target in written:
            path = home / target["rel"]
            _atomic_write_bytes(path, target["current"], stat.S_IMODE(os.lstat(path).st_mode))
        for backup, previous_bytes in saved:
            _restore_backup(backup, previous_bytes)
        reason = str(error) if isinstance(error, HostRulesError) else error.__class__.__name__
        return {"status": "blocked", "reason": "写入失败，已恢复原文件：" + reason, "entries": [], "paths": []}
    return {"status": "applied", "entries": entries, "paths": [t["path"] for t in changes]}


def _remove_block(text, start, end):
    """Drop the block plus the newline after it and one blank-line separator."""
    if text[end:end + 1] == "\n":
        end += 1
    if text[start - 1:start] == "\n" and text[start - 2:start - 1] == "\n":
        start -= 1
    return text[:start] + text[end:]


def _rollback_one(home, entry):
    """Undo one entry; returns (label, note or None)."""
    label = entry.get("path") if isinstance(entry, dict) else None
    if not isinstance(label, str) or not label.startswith("~/") or label[2:] not in CANDIDATES:
        raise HostRulesError(str(label) + "：不是 vibe 管理的用户级规则文件，拒绝回滚")
    rel = label[2:]
    path = _check_path(home, rel)
    read = _read_target(path, rel)
    if read is None:
        raise HostRulesError(label + "：文件不存在，无法回滚")
    data, text = read
    backup = _backup_path(path)
    backup_bytes = _regular_bytes(backup)
    # A backup is ours only if it still holds exactly what this entry saved;
    # anything else belongs to another apply and is neither restored nor removed.
    own_backup = (
        entry.get("restore") != "block"
        and backup_bytes is not None
        and isinstance(entry.get("backup_sha256"), str)
        and _sha(backup_bytes) == entry["backup_sha256"]
    )
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    note = None
    if own_backup and _sha(data) == entry.get("written_sha256"):
        _atomic_write_bytes(path, backup_bytes, mode)
    else:
        found = _find_block(text)
        if found is None:
            note = label + "：标记块已不在，视为已回滚"
        else:
            start, end, _version, intact = found
            if not intact:
                raise HostRulesError(label + "：标记块被人工改过，拒绝回滚")
            _atomic_write_bytes(path, _remove_block(text, start, end).encode("utf-8"), mode)
    if own_backup:
        backup.unlink()
    return label, note


def rollback(home, record):
    """Undo a recorded apply file by file; refusals leave that file untouched."""
    record = record or {}
    entries = (record.get("applied") or []) if record.get("decision") == "apply" else []
    if not entries:
        return {"status": "noop", "reason": "没有已应用的例外，无需回滚", "restored": [], "remaining": []}
    restored, remaining, errors, notes = [], [], [], []
    for entry in entries:
        try:
            label, note = _rollback_one(home, entry)
            restored.append(label)
            if note:
                notes.append(note)
        except HostRulesError as error:
            errors.append(str(error))
            remaining.append(entry)
        except OSError as error:
            errors.append(str(entry.get("path")) + "：回滚失败（" + error.__class__.__name__ + "）")
            remaining.append(entry)
    if not errors:
        status = "rolled_back"
    else:
        status = "partial" if restored else "blocked"
    result = {"status": status, "restored": restored, "remaining": remaining, "notes": notes}
    if errors:
        result["reason"] = "；".join(errors)
    return result


def _record_path(root):
    path = Path(root) / RECORD_RELATIVE
    for candidate in (path.parent, path):
        if candidate.is_symlink():
            raise HostRulesError(".vibe/host-rules.json：路径含符号链接，拒绝读写")
    if os.path.lexists(path) and not path.is_file():
        raise HostRulesError(".vibe/host-rules.json：不是普通文件")
    return path


def read_record(root):
    path = _record_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HostRulesError(".vibe/host-rules.json 无法读取（" + error.__class__.__name__ + "）") from None
    if not isinstance(data, dict) or data.get("decision") not in DECISIONS:
        raise HostRulesError(".vibe/host-rules.json 内容无效")
    return data


def write_record(root, decision, applied=None):
    path = _record_path(root)
    record = {
        "decision": decision,
        "snippet_version": SNIPPET_VERSION,
        "answered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if applied:
        record["applied"] = applied
    descriptor, temporary = tempfile.mkstemp(prefix=".host-rules.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record


def _file_status(home, rel):
    try:
        read = _read_target(_check_path(home, rel), rel)
    except (HostRulesError, OSError):
        return "unreadable"
    if read is None:
        return "absent"
    try:
        found = _find_block(read[1])
    except HostRulesError:
        return "modified"
    if found is None:
        return "not_applied"
    start, end, _version, intact = found
    if not intact:
        return "modified"
    return "current" if read[1][start:end] == CURRENT_BLOCK else "stale"


def status(root, home):
    """Read-only facts for ``vibe doctor``: decision plus per-file block state."""
    try:
        record = read_record(root)
        decision = record["decision"] if record else "unanswered"
    except HostRulesError as error:
        decision, reason = "unknown", str(error)
    else:
        reason = None
    result = {
        "decision": decision,
        "snippet_version": SNIPPET_VERSION,
        "files": {_label(rel): _file_status(home, rel) for rel in CANDIDATES},
    }
    if reason:
        result["reason"] = reason
    return result


def _commands(digest=None):
    commands = [
        "vibe init --confirm --host-rules skip",
        "vibe init --confirm --host-rules apply",
    ]
    if digest:
        commands = ["vibe init --confirm --host-rules apply --host-rules-digest " + digest]
    return commands


def init_step(root, home, choice=None, digest=None):
    """Run the init decision item; returns the ``host_rules`` payload.

    Never raises: any failure becomes ``status: blocked`` with a reason so the
    rest of ``init`` keeps its own result.
    """
    try:
        return _init_step(root, home, choice, digest)
    except HostRulesError as error:
        return {"decision": choice or "unknown", "status": "blocked", "reason": str(error)}
    except (OSError, ValueError) as error:
        return {
            "decision": choice or "unknown", "status": "blocked",
            "reason": "用户级规则步骤失败（" + error.__class__.__name__ + "）",
        }


def _init_step(root, home, choice, digest):
    record = read_record(root)
    decision = record["decision"] if record else "unanswered"
    if choice is None:
        if record is None:
            return {
                "decision": "unanswered",
                "status": "decision_required",
                "question": QUESTION,
                "options": [
                    {"id": "skip", "label": "不应用：不写任何用户级文件，init 照常完成"},
                    {"id": "apply", "label": "应用：先展示差异，凭摘要授权后写入带标记的例外片段（写前备份，可回滚）"},
                ],
                "commands": _commands(),
            }
        return {"decision": decision, "status": "answered"}
    if choice == "skip":
        if decision == "apply":
            return {
                "decision": decision, "status": "blocked",
                "reason": "例外已应用；如要撤销请运行 vibe init --confirm --host-rules rollback",
            }
        if decision != "skip":
            write_record(root, "skip")
        return {"decision": "skip", "status": "answered", "paths": []}
    if choice == "rollback":
        result = rollback(home, record)
        payload = {"decision": decision, "status": result["status"], "paths": result["restored"]}
        if result.get("reason"):
            payload["reason"] = result["reason"]
        if result.get("notes"):
            payload["notes"] = result["notes"]
        if result["status"] == "rolled_back":
            write_record(root, "rolled_back")
            payload["decision"] = "rolled_back"
        elif result["status"] == "partial":
            write_record(root, "apply", result["remaining"])
        return payload
    if choice != "apply":
        return {"decision": decision, "status": "blocked", "reason": "未知选项：" + str(choice)}
    if not digest:
        plan = plan_apply(home)
        if plan["errors"]:
            return {"decision": decision, "status": "blocked", "reason": "；".join(plan["errors"])}
        payload = {
            "decision": decision, "status": "preview", "diff": plan["diff"],
            "digest": plan["digest"], "commands": _commands(plan["digest"]),
            "targets": [t["path"] for t in plan["targets"]],
        }
        if all(t["action"] == "current" for t in plan["targets"]):
            payload["note"] = "用户级规则已是当前版本；运行上面的命令只会登记决定"
        return payload
    previous = record.get("applied") if record and record.get("decision") == "apply" else None
    result = apply(home, digest, previous)
    if result["status"] == "blocked":
        return {"decision": decision, "status": "blocked", "reason": result["reason"]}
    if not (record and record.get("decision") == "apply" and record.get("applied") == result["entries"]):
        write_record(root, "apply", result["entries"])
    return {"decision": "apply", "status": result["status"], "paths": result["paths"]}


def summary_text(payload):
    """One Chinese line for the init text output."""
    state = payload.get("status")
    if state == "decision_required":
        return (
            "待回答：" + QUESTION + "请运行 " + " 或 ".join(payload["commands"])
            + "（apply 会先展示差异，凭摘要授权后才写入）"
        )
    if state == "preview":
        return (
            "用户级规则将写入以下差异：\n" + payload.get("diff", "")
            + "确认后运行：" + payload["commands"][0]
        )
    texts = {
        "answered": "用户级规则决定：" + str(payload.get("decision")),
        "applied": "已写入用户级规则例外：" + "、".join(payload.get("paths", [])) + "（已备份，可回滚）",
        "current": "用户级规则例外已是当前版本，未改动",
        "rolled_back": "已回滚用户级规则例外：" + "、".join(payload.get("paths", []))
        + "".join("；" + note for note in payload.get("notes", [])),
        "noop": "没有已应用的用户级规则例外，无需回滚",
    }
    if state in texts:
        return texts[state]
    return "用户级规则步骤未执行（" + str(state) + "）：" + str(payload.get("reason", ""))
