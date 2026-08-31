"""Read-only, fail-closed gates before worker or authorization actions."""

from dataclasses import dataclass, field
from pathlib import Path
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence


class PreflightBlockedError(ValueError):
    def __init__(self, check_ids):
        self.check_ids = tuple(check_ids)
        super().__init__("preflight is not ready to authorize: " + ", ".join(self.check_ids))


@dataclass(frozen=True)
class Check:
    check_id: str
    status: str
    observed: Mapping[str, Any]
    expected: Any
    evidence_ref: str
    recovery_action: str

    def to_dict(self):
        return {"check_id": self.check_id, "status": self.status,
                "observed": dict(self.observed), "expected": self.expected,
                "evidence_ref": self.evidence_ref, "recovery_action": self.recovery_action}


@dataclass(frozen=True)
class PreflightContext:
    checks: List[Check] = field(default_factory=list)
    observations: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PreflightContext":
        if not isinstance(data, Mapping):
            raise TypeError("preflight context must be an object")
        values = dict(data)
        checks = values.pop("checks", [])
        if checks is None:
            checks = []
        if isinstance(checks, tuple):
            checks = list(checks)
        converted = []
        for item in checks if isinstance(checks, list) else []:
            if isinstance(item, Check):
                converted.append(item)
            elif isinstance(item, Mapping):
                converted.append(Check(**dict(item)))
            else:
                raise TypeError("preflight checks must contain Check values")
        if not isinstance(checks, list):
            raise TypeError("preflight checks must be a list of Check values")
        observations = values.pop("observations", values)
        if not isinstance(observations, Mapping):
            raise TypeError("preflight observations must be an object")
        return cls(converted, dict(observations))


@dataclass(frozen=True)
class PreflightReport:
    status: str
    checks: List[Check]
    read_only: bool = True
    no_actions_taken: bool = True
    schema_version: int = 1
    plan_id: str = ""
    plan_revision: int = 0
    run_id: str = ""
    execution_epoch: int = 0
    generated_at: str = ""

    def to_dict(self):
        return {"schema_version": self.schema_version, "plan_id": self.plan_id,
                "plan_revision": self.plan_revision, "run_id": self.run_id,
                "execution_epoch": self.execution_epoch, "generated_at": self.generated_at,
                "status": self.status, "checks": [item.to_dict() for item in self.checks],
                "read_only": self.read_only, "no_actions_taken": self.no_actions_taken}


def save_preflight_report(path: Path, report: PreflightReport) -> None:
    """Persist a report atomically; this writer never creates a task or run."""
    from .state import _atomic_bytes
    target = Path(path)
    if target.is_symlink():
        raise ValueError("preflight report may not be a symlink")
    payload = (json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    _atomic_bytes(target, payload)


@dataclass(frozen=True)
class ArtifactSetValidation:
    valid: bool
    missing: List[str]
    fr_coverage: str = ""
    ac_coverage: str = ""


def validate_v38_artifact_set(prd_ref: str, spec_ref: str, issues_ref: str,
                              dag_ref: str) -> ArtifactSetValidation:
    """Validate versioned planning references without parsing untrusted YAML as code."""
    missing = []
    expected_revisions = (2, 2, 2, 3)
    texts = []
    for reference, expected_revision in zip((prd_ref, spec_ref, issues_ref, dag_ref), expected_revisions):
        if not isinstance(reference, str) or "@" not in reference:
            missing.append("invalid_ref:" + str(reference))
            continue
        raw_path, raw_revision = reference.rsplit("@", 1)
        path = Path(raw_path)
        if not raw_revision.isdigit() or int(raw_revision) != expected_revision:
            missing.append("invalid_revision:" + str(reference))
        if not path.is_file() or path.is_symlink():
            missing.append(str(path))
        else:
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except OSError as error:
                missing.append(type(error).__name__)
    if not missing:
        try:
            issues = texts[2]
            dag = texts[3]
        except OSError as error:
            missing.append(type(error).__name__)
        else:
            def complete(prefix, start, end, text):
                return all("%s-%d" % (prefix, number) in text for number in range(start, end + 1)) or "%s-%d..%s-%d" % (prefix, start, prefix, end) in text
            if not complete("FR", 801, 811, "\n".join(texts)):
                missing.append("fr_coverage")
            if not complete("AC", 801, 808, "\n".join(texts)):
                missing.append("ac_coverage")
            if not complete("V38", 1, 8, dag):
                missing.append("dag_nodes")
            if "deploy: excluded_by_prd" not in dag or "merge_to_main" not in dag:
                missing.append("action_exclusion")
    return ArtifactSetValidation(not missing, missing, "FR-801..FR-811", "AC-801..AC-808")


def preflight_status(report: PreflightReport) -> str:
    if not isinstance(report, PreflightReport):
        raise TypeError("preflight report is required")
    return "preflight_blocked" if any(
        item.status in {"mismatch", "unknown"} for item in report.checks
    ) else "ready_to_authorize"


def _check(check_id: str, status: str, observed: Any, expected: Any,
           evidence_ref: str = "preflight:context", recovery_action: str = "inspect") -> Check:
    if not isinstance(observed, Mapping):
        observed = {"value": observed}
    return Check(check_id, status, dict(observed), expected, evidence_ref, recovery_action)


def _derived_checks(observations: Mapping[str, Any], project_root: Optional[Path] = None) -> List[Check]:
    """Derive deterministic checks from structured, read-only observations."""
    result: List[Check] = []
    def compare(check_id: str, actual_key: str, expected_key: str):
        if actual_key not in observations and expected_key not in observations:
            return
        actual = observations.get(actual_key)
        expected = observations.get(expected_key)
        if expected is None:
            expected = observations.get("expected_" + actual_key)
        status = "passed" if expected is not None and actual == expected else "mismatch"
        result.append(_check(check_id, status, {"observed": actual}, expected,
                             "preflight:" + check_id, "refresh baseline"))

    compare("base_sha", "base_sha", "expected_base_sha")
    compare("remote_target", "remote_target", "expected_remote_target")

    for key, check_id in (("binding_occupied", "occupied_binding"),
                          ("old_active_writer", "old_active_writer"),
                          ("owned_path_overlap", "owned_path_overlap")):
        if key not in observations:
            continue
        value = observations[key]
        status = "mismatch" if bool(value) else "passed"
        result.append(_check(check_id, status, {"observed": value}, False,
                             "preflight:" + check_id, "release or serialize owner"))

    if "production_entrypoint" in observations:
        entry = observations.get("production_entrypoint")
        allowlist = observations.get("allowlist")
        exists = bool(observations.get("entrypoint_exists"))
        if project_root is not None and isinstance(entry, str):
            relative = entry.split(":", 1)[0]
            candidate = Path(relative)
            if candidate.is_absolute() or ".." in candidate.parts:
                exists = False
            else:
                exists = (project_root / candidate).is_file()
        allowed = isinstance(allowlist, (list, tuple)) and any(
            isinstance(item, str) and item == str(entry).split(":", 1)[0] for item in allowlist
        )
        status = "passed" if exists and allowed else "mismatch"
        result.append(_check("production_entrypoint", status,
                             {"entrypoint": entry, "exists": exists, "allowlisted": allowed},
                             {"exists": True, "allowlisted": True},
                             "preflight:production_entrypoint", "declare a real allowlisted entrypoint"))

    if "capability_status" in observations:
        value = observations.get("capability_status")
        if value == "unknown" or value is None:
            status = "unknown"
        elif value in {"passed", "verified_available", "available"}:
            status = "passed"
        else:
            status = "mismatch"
        result.append(_check("structured_capability", status, {"status": value}, "verified_available",
                             "preflight:provider-capability", "retry structured provider observation"))

    if "baseline_manifest" in observations:
        value = observations.get("baseline_manifest")
        required = {"schema_version", "base_sha", "commands", "collection_count", "import_errors", "scope", "generated_at"}
        status = "passed" if isinstance(value, Mapping) and required.issubset(value) and isinstance(value.get("commands"), list) else "mismatch"
        result.append(_check("baseline_manifest", status, {"present": bool(value)}, "present",
                             "preflight:baseline-health", "generate or restore baseline manifest"))

    if "merge_target_branch" in observations or "expected_merge_target_branch" in observations:
        value = observations.get("merge_target_branch")
        expected = observations.get("expected_merge_target_branch")
        normalized = value.strip().casefold() if isinstance(value, str) else ""
        expected_normalized = expected.strip().casefold() if isinstance(expected, str) else None
        status = "passed" if normalized and normalized not in {"main", "origin/main"} and (expected_normalized is None or normalized == expected_normalized) else "mismatch"
        result.append(_check("merge_target", status, {"branch": value}, expected or "explicit non-main branch",
                             "preflight:git-target", "declare an explicit non-main merge target"))

    return result


def run_preflight(context: PreflightContext) -> PreflightReport:
    if not isinstance(context, PreflightContext):
        raise TypeError("preflight context is required")
    invalid = [item.check_id for item in context.checks if item.status not in {"passed", "mismatch", "unknown"}]
    if invalid:
        checks = [item for item in context.checks if item.check_id not in invalid]
        checks.extend(_check(item, "unknown", {"status": "invalid"}, "passed|mismatch|unknown", "preflight:malformed", "repair check evidence") for item in invalid)
        return PreflightReport("preflight_blocked", checks)
    checks = list(context.checks)
    if not checks:
        project_root = context.observations.get("project_root")
        if project_root is not None:
            project_root = Path(project_root)
        checks = _derived_checks(context.observations, project_root)
    if not checks:
        checks = [_check("preflight_context", "unknown", {"present": False}, "structured observations or checks",
                         "preflight:context", "provide structured preflight evidence")]
    elif not context.checks:
        required = {"base_sha", "remote_target", "occupied_binding", "old_active_writer", "owned_path_overlap", "production_entrypoint", "structured_capability", "baseline_manifest", "merge_target"}
        present = {item.check_id for item in checks}
        checks.extend(_check(item, "unknown", {"present": False}, "observed", "preflight:missing:" + item, "provide structured observation") for item in sorted(required - present))
    report = PreflightReport("pending", checks)
    return PreflightReport(preflight_status(report), report.checks)


def assert_authorizable(report: PreflightReport) -> None:
    if preflight_status(report) != "ready_to_authorize":
        blocked = [item.check_id for item in report.checks if item.status in {"mismatch", "unknown"}]
        raise PreflightBlockedError(blocked)
