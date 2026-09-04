"""Explicit writer ownership checks for parallel DAG nodes."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import List, Sequence


def normalize_project_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\\" in value:
        raise ValueError("project path is invalid")
    path = PurePosixPath(value.strip())
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("project path must remain inside the project")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError("project path is invalid")
    return normalized


@dataclass(frozen=True)
class OwnershipConflict:
    path: str
    nodes: tuple


@dataclass(frozen=True)
class PathOwnershipResult:
    valid: bool
    conflicts: List[OwnershipConflict]
    missing_nodes: List[str]


def validate_path_ownership(nodes: Sequence[object]) -> PathOwnershipResult:
    owners = {}
    missing = []
    for node in nodes:
        node_id = getattr(node, "id", "")
        owned = getattr(node, "owned_paths", None)
        reads = getattr(node, "read_paths", None)
        if not isinstance(owned, list) or not isinstance(reads, list):
            missing.append(node_id)
            owned, reads = [], []
        for path in owned + reads:
            normalize_project_path(path)
        for path in owned:
            normalized = normalize_project_path(path)
            owners.setdefault(normalized, []).append(node_id)
    conflicts = [OwnershipConflict(path, tuple(ids)) for path, ids in sorted(owners.items()) if len(ids) > 1]
    return PathOwnershipResult(not conflicts and not missing, conflicts, missing)


def normalize_allowlist(paths: Sequence[str]) -> List[str]:
    """Normalize a node allowlist and reject paths escaping the project."""
    if not isinstance(paths, (list, tuple)):
        raise ValueError("allowlist must be a list")
    result = [normalize_project_path(item) for item in paths]
    if len(result) != len(set(result)):
        raise ValueError("allowlist contains duplicates")
    return result
