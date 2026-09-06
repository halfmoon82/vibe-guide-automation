"""Current-run manifest and execution-epoch binding for V3.8."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional

from .paths import ProjectPaths
from .state import _atomic_bytes, run_dir


_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA64 = re.compile(r"^[0-9a-fA-F]{64}$")
MANIFEST_SCHEMA_VERSION = 1


def _digest(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(data), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class RunManifest:
    plan_id: str
    plan_revision: int
    run_id: str
    base_sha: str
    target_branch: str
    execution_epoch: int
    authorization_digest: str
    evidence_ref: str
    previous_manifest_digest: str = ""
    created_at: str = ""
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self):
        for name in ("plan_id", "run_id", "target_branch", "evidence_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("%s must be non-empty" % name)
        if not isinstance(self.plan_revision, int) or self.plan_revision < 1:
            raise ValueError("plan_revision must be positive")
        if not _SHA40.fullmatch(self.base_sha):
            raise ValueError("base_sha must be a 40-hex SHA")
        if not isinstance(self.execution_epoch, int) or self.execution_epoch < 0:
            raise ValueError("execution_epoch must be non-negative")
        if self.authorization_digest and not _SHA64.fullmatch(self.authorization_digest):
            raise ValueError("authorization_digest must be a SHA-256 digest")
        if self.previous_manifest_digest and not _SHA64.fullmatch(self.previous_manifest_digest):
            raise ValueError("previous_manifest_digest must be a SHA-256 digest")
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported manifest schema")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RunManifest":
        if not isinstance(data, Mapping):
            raise TypeError("manifest must be an object")
        values = dict(data)
        values.setdefault("schema_version", MANIFEST_SCHEMA_VERSION)
        values.setdefault("previous_manifest_digest", "")
        values.setdefault("created_at", _timestamp())
        expected = {"plan_id", "plan_revision", "run_id", "base_sha", "target_branch",
                    "execution_epoch", "authorization_digest", "evidence_ref",
                    "previous_manifest_digest", "created_at", "schema_version"}
        if set(values) != expected:
            raise ValueError("manifest schema is invalid")
        return cls(**values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "run_id": self.run_id,
            "base_sha": self.base_sha.lower(),
            "target_branch": self.target_branch,
            "execution_epoch": self.execution_epoch,
            "authorization_digest": self.authorization_digest.lower(),
            "evidence_ref": self.evidence_ref,
            "previous_manifest_digest": self.previous_manifest_digest.lower(),
            "created_at": self.created_at,
        }

    def digest(self) -> str:
        return _digest(self.to_dict())


def _manifest_path(paths: ProjectPaths, run_id: str) -> Path:
    directory = run_dir(paths, run_id, create=True)
    path = directory / "run-manifest.json"
    if path.is_symlink():
        raise ValueError("run manifest may not be a symlink")
    return path


def save_run_manifest(paths: ProjectPaths, manifest: RunManifest) -> None:
    path = _manifest_path(paths, manifest.run_id)
    payload = (json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_bytes(path, payload)


def load_run_manifest(paths: ProjectPaths, run_id: str) -> RunManifest:
    path = _manifest_path(paths, run_id)
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.is_symlink():
        raise ValueError("run manifest may not be a symlink")
    return RunManifest.from_mapping(json.loads(path.read_text(encoding="utf-8")))


def advance_execution_epoch(manifest: RunManifest, reason: str) -> RunManifest:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("epoch transition reason is required")
    return RunManifest(
        plan_id=manifest.plan_id, plan_revision=manifest.plan_revision,
        run_id=manifest.run_id, base_sha=manifest.base_sha,
        target_branch=manifest.target_branch, execution_epoch=manifest.execution_epoch + 1,
        authorization_digest="", evidence_ref=manifest.evidence_ref,
        previous_manifest_digest=manifest.digest(), created_at=_timestamp(),
    )
