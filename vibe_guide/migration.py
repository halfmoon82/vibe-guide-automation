"""Small, backup-first migration service for the V2 project layout."""

from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import stat
from typing import Any, Dict, List

from .installation import SDD_SKILL_DEPENDENCIES


TARGET_VERSION = "4.0.0"
_EXCLUDED = {"e2e_mailbox", "e2e-mailbox-verification"}


@dataclass
class MigrationResult:
    status: str
    source_version: str = "2.0.0"
    target_version: str = TARGET_VERSION
    source: str = ""
    destination: str = ""
    backup_path: str = ""
    backup_manifest: Dict[str, Any] = None
    migrated_files: List[str] = None
    idempotent: bool = False
    errors: List[str] = None

    def __post_init__(self):
        if self.backup_manifest is None:
            self.backup_manifest = {}
        if self.migrated_files is None:
            self.migrated_files = []
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict:
        return asdict(self)


def _excluded(relative: Path) -> bool:
    return any(part.casefold() in _EXCLUDED for part in relative.parts)


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _excluded(relative):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink is not migratable: {relative}")
        if path.is_file():
            yield path, relative
        elif not path.is_dir():
            raise ValueError(f"unsupported filesystem object: {relative}")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_entries(value):
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        return None
    if not all(isinstance(e, dict) and isinstance(e.get("path"), str) and isinstance(e.get("sha256"), str) for e in value["files"]):
        return None
    return value["files"]


def _payload_is_complete(payload: Path, entries) -> bool:
    listed = set()
    for entry in entries:
        rel = Path(entry["path"])
        if rel.is_absolute() or ".." in rel.parts or _excluded(rel):
            return False
        path = payload / rel; listed.add(str(rel))
        if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode) or _sha(path) != entry["sha256"]:
            return False
    for path in payload.rglob("*"):
        rel = path.relative_to(payload)
        if path.is_symlink() or (not path.is_dir() and (not path.is_file() or not stat.S_ISREG(path.stat().st_mode))):
            return False
        if path.is_file() and str(rel) not in listed:
            return False
    return True


def _atomic_json(path: Path, value: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _source_root(path: Path) -> Path:
    path = Path(path).resolve(strict=False)
    if not path.exists() or not path.is_dir():
        raise ValueError("migration source must be an existing directory")
    return path


def _backup(source: Path, parent: Path):
    entries = []
    for path, relative in _files(source):
        entries.append((str(relative), _sha(path)))
    fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()[:16]
    backup = parent / ".vibe-migration-backups" / fingerprint
    payload = backup / "payload"
    manifest_path = backup / "manifest.json"
    valid = False
    if manifest_path.is_file() and payload.is_dir():
        try:
            recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
            valid = _manifest_entries(recorded) == [{"path": p, "sha256": h} for p, h in entries]
            valid = valid and _payload_is_complete(payload, recorded["files"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            valid = False
    if not valid:
        shutil.rmtree(backup, ignore_errors=True)
        payload.mkdir(parents=True, exist_ok=True)
        for path, relative in _files(source):
            target = payload / relative; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        manifest = {"source": str(source), "files": [{"path": p, "sha256": h} for p, h in entries]}
        _atomic_json(manifest_path, manifest)
    return backup, {"manifest": str(manifest_path), "files": [{"path": p, "sha256": h} for p, h in entries]}


def restore_backup(backup_path, destination) -> MigrationResult:
    """Restore a backup payload into a directory, without touching its source."""
    try:
        backup = Path(backup_path).resolve()
        target = Path(destination).resolve()
        payload = backup / "payload"
        manifest = backup / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        entries = _manifest_entries(data)
        if not payload.is_dir() or entries is None:
            raise ValueError("backup manifest or payload is invalid")
        if not _payload_is_complete(payload, entries):
            raise ValueError("backup payload contains unlisted or unsafe objects")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
                raise ValueError("backup manifest entry is invalid")
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or _excluded(relative):
                raise ValueError("backup manifest path is invalid")
            path = payload / relative
            if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode) or _sha(path) != entry["sha256"]:
                raise ValueError(f"backup file failed integrity check: {relative}")
        target.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            relative = Path(entry["path"]); path = payload / relative
            out = target / relative; out.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, out)
        return MigrationResult("restored", backup_path=str(backup), destination=str(target), migrated_files=[e["path"] for e in entries])
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return MigrationResult("blocked_invalid", backup_path=str(backup_path), destination=str(destination), errors=[str(error)])


def migrate_v2_to_v310(source, destination) -> MigrationResult:
    try:
        source = Path(os.fspath(source)).resolve(strict=False)
        destination = Path(os.fspath(destination)).resolve(strict=False)
    except (TypeError, ValueError, OSError) as error:
        return MigrationResult("blocked_invalid", errors=[str(error)])
    # Accept either a project root or a `.vibe` root, while preserving the
    # caller's layout in the destination.
    base = source
    backup_path = ""
    backup_manifest = {}
    try:
        base = _source_root(base)
        if destination == source or destination == base or destination.is_relative_to(source):
            raise ValueError("destination must not be inside source")
        marker = destination / ("migration-result.json" if source.name == ".vibe" else ".vibe/migration-result.json")
        source_config = base / ".vibe" / "config.json" if source.name != ".vibe" else base / "config.json"
        try:
            source_data = json.loads(source_config.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("invalid source config") from error
        if not isinstance(source_data, dict) or source_data.get("version") not in {"2.0.0", "3.10.0"}:
            raise ValueError("source is not a V2.0.0 or V3.10.0 project")
        source_version = str(source_data["version"])
        if marker.is_file():
            try:
                recorded = json.loads(marker.read_text(encoding="utf-8"))
                complete = (isinstance(recorded, dict) and recorded.get("status") == "migrated" and
                    recorded.get("target_version") == TARGET_VERSION and recorded.get("source") == str(source) and
                    recorded.get("destination") == str(destination) and isinstance(recorded.get("backup_path"), str) and
                    isinstance(recorded.get("backup_manifest"), dict) and isinstance(recorded.get("migrated_files"), list))
                if complete:
                    backup = Path(recorded["backup_path"])
                    bm = backup / "manifest.json"; payload = backup / "payload"
                    entries = _manifest_entries(recorded["backup_manifest"])
                    complete = bm.is_file() and payload.is_dir() and entries is not None
                    if complete:
                        complete = _payload_is_complete(payload, entries)
                    current = {str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file() and str(p.relative_to(destination)) != str(marker.relative_to(destination))}
                    complete = complete and current == set(recorded["migrated_files"])
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                complete = False
            if complete:
                return MigrationResult("already_current", source_version=str(recorded.get("source_version", source_version)), source=str(source), destination=str(destination), backup_path=recorded["backup_path"], backup_manifest=recorded["backup_manifest"], migrated_files=recorded["migrated_files"], idempotent=True)
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("destination already contains data")
        backup, manifest = _backup(base, destination.parent)
        backup_path, backup_manifest = str(backup), manifest
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.migration-", dir=str(destination.parent)))
        try:
            for path, relative in _files(base):
                out = staging / relative; out.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, out)
            prefix = Path() if source.name == ".vibe" else Path(".vibe")
            for name in ("state.json", "config.json", "session-contract.json"):
                path = staging / prefix / name
                if path.is_file():
                    try:
                        value = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError, json.JSONDecodeError) as error:
                        raise ValueError(f"invalid {name}") from error
                    if not isinstance(value, dict):
                        raise ValueError(f"{name} must be an object")
                    value.setdefault("legacy_workflow_version", value.get("workflow_version", 2))
                    value["version"] = TARGET_VERSION
                    value["workflow_version"] = 4
                    value["execution_mode"] = "sdd_first"
                    value["required_skills"] = [dict(item) for item in SDD_SKILL_DEPENDENCIES]
                    _atomic_json(path, value)
            marker_relative = Path("migration-result.json") if source.name == ".vibe" else Path(".vibe/migration-result.json")
            (staging / marker_relative).parent.mkdir(parents=True, exist_ok=True)
            result = MigrationResult("migrated", source_version=source_version, source=str(source), destination=str(destination), backup_path=str(backup), backup_manifest=manifest, migrated_files=[entry["path"] for entry in manifest["files"]])
            _atomic_json(staging / marker_relative, result.to_dict())
            if destination.exists():
                destination.rmdir()
            os.replace(staging, destination)
            return result
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return MigrationResult("blocked_invalid", source_version=locals().get("source_version", "2.0.0"), source=str(source), destination=str(destination), backup_path=backup_path, backup_manifest=backup_manifest, errors=[str(error)])
