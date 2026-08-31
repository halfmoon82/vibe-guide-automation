"""One immutable baseline-health observation per run."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Dict, List, Sequence

from .paths import ProjectPaths
from .state import _atomic_bytes, run_dir


@dataclass(frozen=True)
class BaselineHealthManifest:
    base_sha: str
    commands: List[Dict[str, Any]]
    collection_count: int
    import_errors: int
    scope: str
    generated_at: str
    schema_version: int = 1

    def to_dict(self):
        return {"schema_version": self.schema_version, "base_sha": self.base_sha,
                "commands": self.commands, "collection_count": self.collection_count,
                "import_errors": self.import_errors, "scope": self.scope,
                "generated_at": self.generated_at}

    def digest(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


def build_baseline_health(root: Path, base_sha: str, commands: Sequence[Sequence[str]]) -> BaselineHealthManifest:
    records = []
    for command in commands:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("baseline command must be a non-empty string list")
        started = time.monotonic()
        result = subprocess.run(list(command), cwd=str(root), text=True,
                               capture_output=True, check=False)
        records.append({"command": list(command), "exit_code": result.returncode,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "stdout_digest": hashlib.sha256(result.stdout.encode()).hexdigest(),
                        "stderr_digest": hashlib.sha256(result.stderr.encode()).hexdigest()})
    failed = sum(record["exit_code"] != 0 for record in records)
    return BaselineHealthManifest(base_sha=base_sha, commands=records,
                                  collection_count=len(records), import_errors=failed,
                                  scope="passed" if not failed else "out_of_scope",
                                  generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def _path(paths: ProjectPaths, run_id: str) -> Path:
    path = run_dir(paths, run_id, create=True) / "baseline-health.json"
    if path.is_symlink():
        raise ValueError("baseline health may not be a symlink")
    return path


def save_baseline_health(paths: ProjectPaths, run_id: str, manifest: BaselineHealthManifest) -> None:
    _atomic_bytes(_path(paths, run_id), (json.dumps(manifest.to_dict(), sort_keys=True) + "\n").encode())


def load_baseline_health(paths: ProjectPaths, run_id: str) -> BaselineHealthManifest:
    path = _path(paths, run_id)
    if not path.exists():
        raise FileNotFoundError(str(path))
    return BaselineHealthManifest(**json.loads(path.read_text(encoding="utf-8")))
