"""Fail-closed migration of legacy V2 planning evidence.

Migration writes only a new revision directory.  The source directory and its
authorization lineage remain immutable historical evidence.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from typing import Any, Dict, Optional

from .diagnostics import LegacyPlanDiagnostic, diagnose_legacy_plan


MIGRATION_SCHEMA_VERSION = 1
_DIGEST = "0123456789abcdef"


def _trusted_absolute(path: Any) -> Path:
    """Make a lexical absolute path, expanding only macOS system aliases."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    for alias in (Path("/var"), Path("/tmp")):
        try:
            remainder = absolute.relative_to(alias)
        except ValueError:
            continue
        if alias.is_symlink():
            target = Path(os.readlink(alias))
            if not target.is_absolute():
                target = alias.parent / target
            absolute = target.joinpath(*remainder.parts)
        break
    return absolute


@dataclass(frozen=True)
class MigrationReport:
    source: str
    plan_id: Optional[str]
    old_revision: Optional[int]
    new_revision: Optional[int]
    status: str
    diagnostic: LegacyPlanDiagnostic
    superseded_marker: Dict[str, Any]
    preserved_evidence: Dict[str, Dict[str, Any]]
    current_authorization_digest: Optional[str] = None
    remediation: str = ""

    @property
    def preserved_evidence_map(self) -> Dict[str, Dict[str, Any]]:
        return self.preserved_evidence

    @property
    def planning_required(self) -> bool:
        return self.status == "planning_required"

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["diagnostic"] = asdict(self.diagnostic)
        result["schema_version"] = MIGRATION_SCHEMA_VERSION
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MigrationReport":
        if not isinstance(data, dict) or data.get("schema_version") != MIGRATION_SCHEMA_VERSION:
            raise ValueError("migration report schema is invalid")
        diagnostic_data = data.get("diagnostic")
        if not isinstance(diagnostic_data, dict):
            raise ValueError("migration diagnostic is invalid")
        diagnostic = LegacyPlanDiagnostic(**diagnostic_data)
        names = (
            "source", "plan_id", "old_revision", "new_revision", "status",
            "superseded_marker", "preserved_evidence", "current_authorization_digest",
            "remediation",
        )
        if any(name not in data for name in names):
            raise ValueError("migration report fields are incomplete")
        values = {name: data[name] for name in names}
        if not isinstance(values["source"], str) or not isinstance(values["status"], str):
            raise ValueError("migration report identity is invalid")
        if not isinstance(values["superseded_marker"], dict) or not isinstance(values["preserved_evidence"], dict):
            raise ValueError("migration report evidence is invalid")
        return cls(diagnostic=diagnostic, **values)


def _safe_root(path: Any) -> Path:
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("legacy plan directory must be a real directory")
    probe = root.absolute()
    while probe != probe.parent:
        if probe.is_symlink() and str(probe) not in {"/var", "/tmp"}:
            raise ValueError("legacy plan directory may not traverse symlinks")
        probe = probe.parent
    return root.resolve()


def _read_plan(root: Path) -> Dict[str, Any]:
    path = root / "plan.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("legacy plan.json is unreadable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("legacy plan.json is unreadable") from error
    if not isinstance(value, dict):
        raise ValueError("legacy plan.json must be an object")
    return value


def _read_optional_json(path: Path) -> Optional[Dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _evidence_map(root: Path) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise ValueError("legacy evidence may not contain symlinks: " + candidate.relative_to(root).as_posix())
        if not candidate.is_file():
            continue
        payload = candidate.read_bytes()
        result[candidate.relative_to(root).as_posix()] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "historical": True,
            "read_only": True,
        }
    return result


def inspect_legacy_plan(plan_dir: Any) -> MigrationReport:
    """Return a read-only migration report for a legacy plan directory."""
    root = _safe_root(plan_dir)
    diagnostic = diagnose_legacy_plan(root)
    plan: Optional[Dict[str, Any]]
    try:
        plan = _read_plan(root)
    except ValueError:
        plan = None
    plan_id = diagnostic.plan_id
    old_revision: Optional[int] = None
    if isinstance(plan, dict):
        version = plan.get("version", plan.get("plan_version"))
        if isinstance(version, int) and not isinstance(version, bool) and version >= 1:
            old_revision = version
    new_revision = old_revision + 1 if old_revision is not None else None
    preserved = _evidence_map(root)
    marker = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "status": "superseded",
        "read_only": True,
        "source_plan_id": plan_id,
        "source_revision": old_revision,
        "superseded_by_revision": new_revision,
        "authorization_current": False,
        "reason": "legacy evidence is retained for audit only",
    }
    return MigrationReport(
        source=str(root),
        plan_id=plan_id,
        old_revision=old_revision,
        new_revision=new_revision,
        status=diagnostic.status,
        diagnostic=diagnostic,
        superseded_marker=marker,
        preserved_evidence=preserved,
        current_authorization_digest=None,
        remediation=diagnostic.remediation,
    )


def _canonical_destination(path: Path) -> Path:
    """Return a lexical absolute destination; descriptor opens enforce reality."""
    candidate = Path(path)
    absolute = Path(os.path.abspath(os.fspath(candidate)))
    probe = absolute
    while True:
        if probe.is_symlink() and str(probe) not in {"/var", "/tmp"}:
            raise ValueError("migration destination may not traverse symlinks")
        if probe == probe.parent:
            break
        probe = probe.parent
    return absolute


def _open_relative_directory(parent_fd: int, parts: Any) -> int:
    """Open/create a child directory using only a validated parent descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.dup(parent_fd)
    try:
        for part in parts:
            part = os.fspath(part)
            if part in ("", "."):
                continue
            if part == ".." or os.sep in part or (os.altsep and os.altsep in part):
                raise ValueError("relative directory escapes its parent")
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_confined_directory(path: Path) -> int:
    """Open/create a directory tree with no-follow directory descriptors."""
    absolute = _trusted_absolute(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for part in absolute.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _assert_destination_root(destination: Path) -> int:
    if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
        raise ValueError("migration destination must be a real directory")
    return _open_confined_directory(destination)


def _atomic_bytes_at(parent_fd: int, name: str, data: bytes) -> None:
    """Atomically replace ``name`` while retaining the parent dirfd."""
    if not isinstance(name, str) or not name or name in {".", ".."} or os.sep in name:
        raise ValueError("atomic filename must be a direct child")
    temporary = "." + name + "." + uuid.uuid4().hex
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = None
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _atomic_json_at(parent_fd: int, name: str, value: Dict[str, Any]) -> None:
    _atomic_bytes_at(
        parent_fd,
        name,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    parent = _trusted_absolute(path.parent)
    parent_fd = _open_confined_directory(parent)
    try:
        _atomic_json_at(parent_fd, path.name, value)
    finally:
        os.close(parent_fd)


def _ensure_confined_dir(destination_root: Path, path: Path) -> None:
    target = _trusted_absolute(path)
    root = _trusted_absolute(destination_root)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("migration destination escapes its root") from error
    descriptor = _open_confined_directory(target)
    os.close(descriptor)


def _copy_file_no_follow(
    source: Path,
    destination: Path,
    expected_digest: str,
    parent_fd: Optional[int] = None,
) -> None:
    if source.is_symlink() or not source.is_file() or (
        parent_fd is None and destination.is_symlink()
    ):
        raise ValueError("legacy evidence changed during migration")
    source_fd = os.open(str(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    source_stat = os.fstat(source_fd)
    if not stat.S_ISREG(source_stat.st_mode):
        os.close(source_fd)
        raise ValueError("legacy evidence must be a regular file")
    owns_parent = parent_fd is None
    if owns_parent:
        try:
            parent_fd = _open_confined_directory(destination.parent)
        except Exception:
            os.close(source_fd)
            raise
    assert parent_fd is not None
    temporary = "." + destination.name + "." + uuid.uuid4().hex
    descriptor = None
    digest = hashlib.sha256()
    try:
        try:
            existing = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ValueError("legacy evidence destination may not be a symlink")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != expected_digest:
            raise ValueError("legacy evidence changed during migration")
        os.replace(temporary, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = None
        os.fsync(parent_fd)
    finally:
        os.close(source_fd)
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        if owns_parent:
            os.close(parent_fd)


def _lineage_key(key: Any) -> str:
    normalized = str(key).strip().casefold().replace("-", "_")
    return "_".join(part for part in normalized.split("_") if part)


def _compact_key(key: Any) -> str:
    """Normalize snake, kebab, and camel-case aliases to one key form."""
    return "".join(character for character in str(key).casefold() if character.isalnum())


def _is_legacy_lineage_key(key: Any, authorization_context: bool = False) -> bool:
    compact = _compact_key(key)
    terms = (
        "authorization",
        "auth",
        "card",
        "confirmation",
        "audit",
        "binding",
        "decision",
        "authorized",
    )
    if compact in terms or any(term in compact for term in terms):
        return True
    if "digest" in compact and (
        authorization_context
        or any(term in compact for term in terms)
        or "contract" in compact
        or compact == "digest"
    ):
        return True
    if "status" in compact and any(term in compact for term in terms):
        return True
    return False


def _is_lineage_container_key(key: Any) -> bool:
    compact = _compact_key(key)
    return compact in {
        "authorization",
        "auth",
        "card",
        "confirmation",
        "nodecontract",
        "authorizationcontract",
        "authcontract",
    }


def _is_revision_alias_key(key: Any) -> bool:
    compact = _compact_key(key)
    if compact in {"schemaversion", "eventversion"}:
        return False
    if compact in {
        "version",
        "planversion",
        "revision",
        "planrevision",
        "revisionalias",
        "planrevisionalias",
        "revisionnumber",
        "planrevisionnumber",
        "revisionid",
        "planrevisionid",
        "rev",
        "planrev",
    }:
        return True
    if "revision" in compact or compact.startswith("rev"):
        return True
    return "version" in compact and compact not in {"schemaversion", "eventversion"}


def _legacy_lineage_values(value: Any, authorization_context: bool = False) -> set:
    values = set()
    if isinstance(value, dict):
        for key, item in value.items():
            nested = authorization_context or _is_lineage_container_key(key) or _is_legacy_lineage_key(key)
            if nested:
                if isinstance(item, str):
                    values.add(item)
                values.update(_legacy_lineage_values(item, True))
            else:
                values.update(_legacy_lineage_values(item, authorization_context))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.update(_legacy_lineage_values(item, authorization_context))
    elif authorization_context and isinstance(value, str):
        values.add(value)
    return values


def _strip_authorization_material(
    value: Any,
    authorization_context: bool = False,
    legacy_values: Optional[set] = None,
) -> Any:
    """Remove legacy authorization lineage keys and their values."""
    if legacy_values is None:
        legacy_values = set()
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            cleaned = _strip_authorization_material(
                item, authorization_context, legacy_values
            )
            if cleaned is None and isinstance(item, (str, int, float, bool)):
                continue
            result.append(cleaned)
        return tuple(result) if isinstance(value, tuple) else result
    if not isinstance(value, dict):
        if isinstance(value, (str, int, float, bool)) and str(value) in legacy_values:
            return None
        return value
    result: Dict[str, Any] = {}
    for key, item in value.items():
        normalized = _lineage_key(key)
        if _is_revision_alias_key(key) or _is_legacy_lineage_key(
            key, authorization_context
        ):
            continue
        nested = authorization_context or _is_lineage_container_key(normalized)
        cleaned = _strip_authorization_material(item, nested, legacy_values)
        if cleaned is None and isinstance(item, (str, int, float, bool)):
            continue
        result[key] = cleaned
    return result


def migrate_legacy_plan(plan_dir: Any, destination: Any = None) -> MigrationReport:
    """Inspect, and optionally materialize, a new revision from legacy evidence."""
    report = inspect_legacy_plan(plan_dir)
    if destination is None:
        return report
    source_root = _safe_root(plan_dir)
    target = _canonical_destination(Path(destination))
    try:
        _trusted_absolute(target).relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("migration destination may not be inside the legacy plan")
    target_fd = _assert_destination_root(target)
    try:
        if target.is_symlink():
            raise ValueError("migration destination changed during validation")
        _atomic_json_at(target_fd, "superseded.json", report.superseded_marker)
        _atomic_json_at(target_fd, "migration-report.json", report.to_dict())
        if report.status != "blocked_unknown":
            source_plan = _read_plan(source_root)
            lineage_values = set(_legacy_lineage_values(source_plan))
            for artifact_name in (
                "authorization-card.json",
                "authorization.json",
                "plan-confirmation.json",
                "dag-audit.json",
            ):
                artifact = _read_optional_json(source_root / artifact_name)
                if artifact is not None:
                    lineage_values.update(_legacy_lineage_values(artifact))
            new_plan = _strip_authorization_material(
                source_plan, legacy_values=lineage_values
            )
            new_plan["version"] = report.new_revision
            new_plan["plan_version"] = report.new_revision
            new_plan["status"] = "planning_required"
            _atomic_json_at(target_fd, "plan.json", new_plan)
        evidence_root_fd = _open_relative_directory(target_fd, ("legacy-evidence",))
        try:
            for relative, metadata in report.preserved_evidence.items():
                relative_path = Path(relative)
                if (
                    relative_path.is_absolute()
                    or not relative_path.parts
                    or ".." in relative_path.parts
                ):
                    raise ValueError("legacy evidence path escapes its root")
                source_file = source_root.joinpath(*relative_path.parts)
                parent_fd = _open_relative_directory(
                    evidence_root_fd, relative_path.parts[:-1]
                )
                try:
                    _copy_file_no_follow(
                        source_file,
                        relative_path,
                        metadata["sha256"],
                        parent_fd=parent_fd,
                    )
                finally:
                    os.close(parent_fd)
        finally:
            os.close(evidence_root_fd)
        _atomic_json_at(target_fd, "evidence-map.json", report.preserved_evidence)
    finally:
        os.close(target_fd)
    return report


def load_migration_report(path: Any) -> MigrationReport:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise FileNotFoundError(str(target))
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("migration report is not valid JSON") from error
    return MigrationReport.from_dict(value)


def superseded_marker(plan_dir: Any) -> Dict[str, Any]:
    """Return only the read-only marker; this helper never writes."""
    return inspect_legacy_plan(plan_dir).superseded_marker


__all__ = [
    "MIGRATION_SCHEMA_VERSION", "MigrationReport", "inspect_legacy_plan",
    "migrate_legacy_plan", "load_migration_report", "superseded_marker",
]
