"""Small, evidence-bound capability contracts shared by monitor sessions."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Mapping, Optional


CONTRACT_SCHEMA_VERSION = 1
CONTRACT_TTL = timedelta(hours=1)
CONTRACT_MAX_BYTES = 64 * 1024
CAPABILITY_STATUSES = frozenset(
    {
        "verified_available",
        "not_exposed",
        "permission_denied",
        "probe_failed",
        "unknown_timeout",
        "unknown",
        "stale",
    }
)
_HEX64 = set("0123456789abcdef")


def _now(value: Optional[datetime] = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(timezone.utc).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return _now(value).isoformat()


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a timestamp" % field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("%s is not a valid timestamp" % field) from error
    return _now(parsed)


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        item not in _HEX64 for item in value.lower()
    ):
        raise ValueError("%s must be a SHA-256 digest" % field)
    return value.lower()


def _validate_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % field)
    if "\x00" in value:
        raise ValueError("%s contains NUL" % field)
    return value.strip()


@dataclass(frozen=True)
class CapabilityFact:
    name: str
    status: str
    scope: str
    route: str
    evidence_ref: str
    checked_at: str
    expires_at: str

    def __post_init__(self):
        object.__setattr__(self, "name", _validate_text(self.name, "name"))
        if self.status not in CAPABILITY_STATUSES:
            raise ValueError("unsupported capability status")
        object.__setattr__(self, "scope", _validate_text(self.scope, "scope"))
        if not isinstance(self.route, str) or "\x00" in self.route:
            raise ValueError("route must be a string without NUL")
        object.__setattr__(
            self,
            "evidence_ref",
            _validate_text(self.evidence_ref, "evidence_ref"),
        )
        object.__setattr__(
            self,
            "checked_at",
            _timestamp(_parse_timestamp(self.checked_at, "checked_at")),
        )
        object.__setattr__(
            self,
            "expires_at",
            _timestamp(_parse_timestamp(self.expires_at, "expires_at")),
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "scope": self.scope,
            "route": self.route,
            "evidence_ref": self.evidence_ref,
            "checked_at": self.checked_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityFact":
        if not isinstance(data, Mapping):
            raise TypeError("capability fact must be an object")
        required = {
            "name",
            "status",
            "scope",
            "route",
            "evidence_ref",
            "checked_at",
            "expires_at",
        }
        if set(data) != required:
            raise ValueError("capability fact fields are invalid")
        return cls(**dict(data))


@dataclass(frozen=True)
class CapabilityContract:
    schema_version: int
    project_digest: str
    provider: str
    host_id: str
    scope: str
    capabilities: Dict[str, CapabilityFact]
    contract_digest: str
    expires_at: str

    def __post_init__(self):
        if self.schema_version != CONTRACT_SCHEMA_VERSION:
            raise ValueError("unsupported capability contract schema")
        object.__setattr__(
            self,
            "project_digest",
            _validate_digest(self.project_digest, "project_digest"),
        )
        object.__setattr__(self, "provider", _validate_text(self.provider, "provider"))
        object.__setattr__(self, "host_id", _validate_text(self.host_id, "host_id"))
        object.__setattr__(self, "scope", _validate_text(self.scope, "scope"))
        if not isinstance(self.capabilities, dict):
            raise TypeError("capabilities must be an object")
        normalized = {}
        for name, fact in self.capabilities.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("capability name must be non-empty")
            if not isinstance(fact, CapabilityFact):
                raise TypeError("capability values must be CapabilityFact")
            if fact.name != name:
                raise ValueError("capability name does not match its key")
            normalized[name] = fact
        object.__setattr__(self, "capabilities", normalized)
        if self.contract_digest:
            object.__setattr__(
                self,
                "contract_digest",
                _validate_digest(self.contract_digest, "contract_digest"),
            )
        elif not isinstance(self.contract_digest, str):
            raise ValueError("contract_digest must be a string")
        object.__setattr__(
            self,
            "expires_at",
            _timestamp(_parse_timestamp(self.expires_at, "expires_at")),
        )

    def _payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_digest": self.project_digest,
            "provider": self.provider,
            "host_id": self.host_id,
            "scope": self.scope,
            "capabilities": {
                name: self.capabilities[name].to_dict()
                for name in sorted(self.capabilities)
            },
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> Dict[str, Any]:
        result = self._payload()
        result["contract_digest"] = self.contract_digest
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityContract":
        if not isinstance(data, Mapping):
            raise TypeError("capability contract must be an object")
        required = {
            "schema_version",
            "project_digest",
            "provider",
            "host_id",
            "scope",
            "capabilities",
            "contract_digest",
            "expires_at",
        }
        if set(data) != required or not isinstance(data["capabilities"], Mapping):
            raise ValueError("capability contract fields are invalid")
        facts = {
            str(name): CapabilityFact.from_dict(fact)
            for name, fact in data["capabilities"].items()
        }
        candidate = cls(
            schema_version=data["schema_version"],
            project_digest=data["project_digest"],
            provider=data["provider"],
            host_id=data["host_id"],
            scope=data["scope"],
            capabilities=facts,
            contract_digest=data["contract_digest"],
            expires_at=data["expires_at"],
        )
        if candidate.contract_digest != _digest(candidate._payload()):
            raise ValueError("capability contract digest mismatch")
        return candidate


def _project_digest(project_root: Path) -> str:
    root = Path(project_root).expanduser().resolve(strict=False)
    return _digest({"project_root": str(root)})


def build_contract(
    project_root: Path,
    provider: str = "unknown",
    host_id: str = "unknown",
    facts: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> CapabilityContract:
    checked = _now(now)
    expires = checked + CONTRACT_TTL
    raw_facts = facts or {}
    if not isinstance(raw_facts, Mapping):
        raise TypeError("facts must be an object")
    normalized = {}
    for name, raw in raw_facts.items():
        name = _validate_text(name, "capability name")
        if isinstance(raw, CapabilityFact):
            fact = raw
            if fact.name != name:
                raise ValueError("capability name does not match its key")
        elif isinstance(raw, Mapping):
            if set(raw) - {
                "status",
                "scope",
                "route",
                "evidence_ref",
                "checked_at",
                "expires_at",
            }:
                raise ValueError("capability fact has unsupported fields")
            fact = CapabilityFact(
                name=name,
                status=raw.get("status", "unknown"),
                scope=raw.get("scope", "init"),
                route=raw.get("route", ""),
                evidence_ref=raw.get("evidence_ref", "contract:init:" + name),
                checked_at=raw.get("checked_at", _timestamp(checked)),
                expires_at=raw.get("expires_at", _timestamp(expires)),
            )
        else:
            raise TypeError("capability fact must be an object")
        normalized[name] = fact
    candidate = CapabilityContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        project_digest=_project_digest(Path(project_root)),
        provider=provider,
        host_id=host_id,
        scope="project",
        capabilities=normalized,
        contract_digest="",
        expires_at=_timestamp(expires),
    )
    return replace(candidate, contract_digest=_digest(candidate._payload()))


def contract_path(paths) -> Path:
    directory = paths.vibe_dir
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError(".vibe must be a regular directory")
    return directory / "session-contract.json"


def _ensure_project(contract: CapabilityContract, paths) -> None:
    expected = _project_digest(paths.root)
    if contract.project_digest != expected:
        raise ValueError("capability contract project does not match current project")


def save_contract(paths, contract: CapabilityContract) -> Path:
    if not isinstance(contract, CapabilityContract):
        raise TypeError("contract must be a CapabilityContract")
    _ensure_project(contract, paths)
    target = contract_path(paths)
    directory = target.parent
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("contract directory is invalid")
    directory.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("capability contract may not be a symlink")
    encoded = (
        json.dumps(
            contract.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > CONTRACT_MAX_BYTES:
        raise ValueError("capability contract exceeds the size bound")
    if target.is_file() and target.read_bytes() == encoded:
        return target
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".session-contract.", dir=str(directory)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(target))
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def load_contract(paths, now: Optional[datetime] = None) -> CapabilityContract:
    target = contract_path(paths)
    if target.is_symlink() or not target.is_file():
        raise ValueError("capability contract is missing or not a regular file")
    raw = target.read_bytes()
    if len(raw) > CONTRACT_MAX_BYTES:
        raise ValueError("capability contract exceeds the size bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("capability contract is invalid") from error
    contract = CapabilityContract.from_dict(data)
    _ensure_project(contract, paths)
    if now is not None:
        _now(now)
    return contract


def capability_status(
    contract: CapabilityContract,
    name: str,
    now: Optional[datetime] = None,
) -> str:
    if not isinstance(contract, CapabilityContract):
        raise TypeError("contract must be a CapabilityContract")
    current = _now(now)
    if _parse_timestamp(contract.expires_at, "expires_at") <= current:
        return "stale"
    fact = contract.capabilities.get(name)
    if fact is None:
        return "unknown"
    if _parse_timestamp(fact.expires_at, "expires_at") <= current:
        return "stale"
    return fact.status

