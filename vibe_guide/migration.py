"""Backup helpers used by `vibe init` when it upgrades an old state.json."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import stat


_EXCLUDED = {"e2e_mailbox", "e2e-mailbox-verification"}


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
