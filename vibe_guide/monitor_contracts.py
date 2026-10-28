"""Extracted from monitor.py (behavior unchanged)."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .authorization import contract_invariant_entries
from .config import load_project_config
from .state import RunSnapshot


def _command_matches_blacklist(command: str, blacklist) -> bool:
    """Prefix match on whitespace-split tokens.

    A command hits a blacklist entry when its first ``len(entry_tokens)``
    tokens equal the entry's tokens exactly.
    """
    tokens = str(command).split()
    for entry in blacklist:
        entry_tokens = str(entry).split()
        if entry_tokens and tokens[: len(entry_tokens)] == entry_tokens:
            return True
    return False


#: Compile/build command prefixes that can never prove a *behavioral*
#: invariant (they only prove the code compiles).  Built-in and not reducible
#: through configuration; projects may append entries via
#: ``.vibe/config.json`` ``behavioral_command_blacklist_extra``.
BEHAVIORAL_COMPILE_COMMAND_BLACKLIST = frozenset({
    "tsc",
    "vue-tsc",
    "vite build",
    "webpack",
    "rollup",
    "esbuild",
    "npm run build",
    "pnpm build",
    "yarn build",
    "next build",
    "ng build",
})


# ISSUE-140 (design section 3.3): contract-declared invariant test commands
# are executed by the monitor inside the node writer worktree.  Commands come
# from the already-authorized node contract and execution never leaves the
# contract writer worktree, so this introduces no new authorization surface.
DEFAULT_CONTRACT_TEST_TIMEOUT_SECONDS = 120


def _contract_test_entries(monitor, node_id: str):
    """Collect executable invariant entries from the node contract.

    Missing/non-list/empty ``invariants`` yields an empty list, which is
    what keeps contracts without invariants completely behavior-neutral.
    An invalid ``timeout_seconds`` is a contract error and is reported
    through the returned error string instead of being guessed at.
    """
    node = monitor.nodes[node_id]
    contract = node.contract if isinstance(node.contract, dict) else {}
    entries = []
    for index, item in contract_invariant_entries(contract):
        invariant_id = item.get("id")
        if not isinstance(invariant_id, str) or not invariant_id.strip():
            invariant_id = "invariants[{}]".format(index)
        kind = item.get("kind")
        kind = kind.strip() if isinstance(kind, str) and kind.strip() else None
        timeout_seconds = item.get(
            "timeout_seconds", DEFAULT_CONTRACT_TEST_TIMEOUT_SECONDS
        )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds < 1
        ):
            return None, (
                "invariant {} has an invalid timeout_seconds value "
                "(contract error)".format(invariant_id.strip())
            )
        entries.append(
            {
                "invariant_id": invariant_id.strip(),
                "command": item["test_command"],
                "kind": kind,
                "timeout_seconds": timeout_seconds,
            }
        )
    return entries, None


def _record_contract_test_execution(
    monitor,
    snapshot: RunSnapshot,
    node_id: str,
    phase: str,
    entry: Dict[str, Any],
    exit_code: Optional[int],
    output_tail: str,
    worktree: Path,
    active: Optional[Dict[str, Any]] = None,
) -> None:
    # The execution detail rides under ``proof``: it is a structured
    # result (identifiers, exit code, bounded output tail), and the
    # durable event schema only preserves structured values under
    # pass-through keys.  run_id/node_id/phase stay top-level.
    monitor._record(
        snapshot,
        "contract_test_execution",
        {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "phase": phase,
            "proof": {
                "invariant_id": entry["invariant_id"],
                "command": entry["command"],
                "exit_code": exit_code,
                "output_tail": output_tail,
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "worktree": str(worktree),
            },
        },
        active,
    )


def _execute_contract_test_commands(
    monitor,
    snapshot: RunSnapshot,
    node_id: str,
    phase: str,
    active: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Run contract-declared invariant test commands for one node.

    No new authorization surface: every command comes from the
    already-authorized node contract and execution is confined to the
    contract writer worktree.  ``phase`` is ``"acceptance"`` (commands
    must pass) or ``"expected_red"`` (commands must fail before the
    implementation exists).  Returns ``None`` on success, otherwise a
    failure reason; every executed (or refused) command is recorded as a
    ``contract_test_execution`` event first, so a rejection can never be
    forged into a pass.
    """
    entries, contract_error = monitor._contract_test_entries(node_id)
    if contract_error is not None:
        return contract_error
    if not entries:
        return None
    current = snapshot.nodes[node_id]
    worktree = monitor._worktree_path(current)
    if not worktree.is_dir():
        return (
            "writer worktree {} does not exist; contract test execution "
            "is confined to it".format(worktree)
        )
    blacklist = frozenset()
    if any(entry["kind"] == "behavioral" for entry in entries):
        # Fail closed: an unreadable project config means the effective
        # blacklist is unknown, so behavioral invariants cannot run.
        try:
            project_config = load_project_config(monitor.paths.root)
        except ValueError as error:
            return (
                "behavioral command blacklist cannot be resolved from "
                "the project config: {}".format(error)
            )
        blacklist = BEHAVIORAL_COMPILE_COMMAND_BLACKLIST | set(
            project_config.behavioral_command_blacklist_extra
        )
    for entry in entries:
        if entry["kind"] == "behavioral" and _command_matches_blacklist(
            entry["command"], blacklist
        ):
            note = (
                "behavioral invariant test_command matches the compile "
                "command blacklist"
            )
            monitor._record_contract_test_execution(
                snapshot, node_id, phase, entry, None,
                "not executed: " + note, worktree, active,
            )
            return "invariant {}: {} ({!r})".format(
                entry["invariant_id"], note, entry["command"]
            )
        try:
            completed = subprocess.run(
                entry["command"],
                shell=True,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=entry["timeout_seconds"],
            )
            exit_code: Optional[int] = completed.returncode
            output_tail = (
                (completed.stdout or "") + "\n" + (completed.stderr or "")
            )[-2000:]
        except subprocess.TimeoutExpired as error:
            exit_code = None
            partial = error.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            output_tail = (
                "timeout after {}s\n".format(entry["timeout_seconds"])
                + str(partial)
            )[-2000:]
        monitor._record_contract_test_execution(
            snapshot, node_id, phase, entry, exit_code, output_tail,
            worktree, active,
        )
        if phase == "expected_red":
            # Red means any non-zero outcome (failure, timeout, missing
            # command); only a clean pass violates the TDD contract.
            if exit_code == 0:
                return (
                    "invariant {} expected red before implementation, "
                    "command passed ({!r})".format(
                        entry["invariant_id"], entry["command"]
                    )
                )
            continue
        if exit_code is None:
            return "invariant {} command {!r} timed out after {}s".format(
                entry["invariant_id"], entry["command"], entry["timeout_seconds"]
            )
        if exit_code != 0:
            return "invariant {} command {!r} exited with code {}".format(
                entry["invariant_id"], entry["command"], exit_code
            )
    return None
