"""Read-only planning diagnostics and legacy migration gates.

The diagnostic layer reports evidence and remediation.  It never upgrades a
legacy authorization card into current authority and never edits the source
plan it inspects.
"""

from dataclasses import dataclass
import fcntl
import hashlib
import json
from pathlib import Path
import os
import tempfile
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Proposal:
    proposed: bool
    content: str = ""
    path: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SkillDiagnostic:
    name: str
    status: str
    reason: str
    proposal: Proposal = Proposal(False)


@dataclass(frozen=True)
class ContractCheck:
    ok: bool
    missing: List[str]
    extra: List[str] = None

    def __post_init__(self):
        if self.extra is None:
            object.__setattr__(self, "extra", [])


@dataclass(frozen=True)
class PlanningGate:
    status: str
    plan_id: str
    missing: List[str]


@dataclass(frozen=True)
class SessionGate:
    status: str
    session_id: str
    request: str
    origin: str
    reason: str = ""


@dataclass(frozen=True)
class LegacyPlanDiagnostic:
    status: str
    plan_id: Optional[str]
    missing: List[str]
    reason: str = ""
    remediation: str = ""

    @property
    def planning_required(self) -> bool:
        return self.status == "planning_required"


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not _regular(path):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_optional_json(path: Path) -> tuple:
    """Return ``(value, present)`` while distinguishing malformed evidence."""
    if path.is_symlink():
        return None, True
    if not path.exists():
        return None, False
    return _read_json(path), True


def _route_bindings(plan: Dict[str, Any], root: Path) -> List[str]:
    """Find missing, malformed, or contradictory route fields.

    Legacy artifacts contain route data at both node and nested contract
    levels.  Values are collected from every spelling and compared; nested
    data is never allowed to silently overwrite a sibling value.
    """
    nodes: Any = None
    nodes_path = root / "nodes.json"
    if nodes_path.is_symlink():
        return ["route:nodes:invalid"]
    if nodes_path.exists():
        try:
            value = json.loads(nodes_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ["route:nodes:invalid"]
        if not isinstance(value, list) or not value:
            return ["route:nodes:invalid"]
        nodes = value
    elif "nodes" in plan:
        if not isinstance(plan.get("nodes"), list) or not plan["nodes"]:
            return ["route:nodes:invalid"]
        nodes = plan["nodes"]
    if not nodes:
        return ["route:{}".format(field) for field in ("adapter_id", "provider", "mode", "project", "host", "worktree", "branch", "allowlist")]
    required = ("adapter_id", "provider", "mode", "project", "host", "worktree", "branch", "allowlist")
    aliases = {
        "adapter_id": ("adapter_id", "adapterId"),
        "provider": ("provider",),
        "mode": ("mode",),
        "project": ("project", "project_id", "projectId"),
        "host": ("host", "host_id", "hostId"),
        "worktree": ("worktree",),
        "branch": ("branch",),
        "allowlist": ("allowlist", "allow_list", "allowList"),
    }
    missing: List[str] = []
    for index, item in enumerate(nodes):
        if not isinstance(item, dict):
            missing.append("route:{}:invalid-node".format(index))
            continue
        node_id = item.get("id", item.get("node_id", str(index)))
        if not isinstance(node_id, str) or not node_id.strip():
            missing.append("route:{}:invalid-node".format(index))
            node_id = str(index)
        sources = [item]
        for nested_key in ("worker_profile", "contract", "route"):
            nested = item.get(nested_key)
            if nested is not None and not isinstance(nested, dict):
                missing.append("route:{}:{}:invalid".format(node_id, nested_key))
            elif isinstance(nested, dict):
                sources.append(nested)
        for field in required:
            names = aliases.get(field, (field,))
            values = []
            invalid = False
            for source in sources:
                for name in names:
                    if name not in source:
                        continue
                    value = source[name]
                    if field == "allowlist":
                        valid = (
                            isinstance(value, list)
                            and bool(value)
                            and all(
                                isinstance(path, str)
                                and bool(path.strip())
                                and not Path(path).is_absolute()
                                and ".." not in Path(path).parts
                                for path in value
                            )
                        )
                    else:
                        valid = isinstance(value, str) and bool(value.strip())
                    if not valid:
                        invalid = True
                    else:
                        values.append(value)
            if invalid:
                missing.append("route:{}:{}:invalid".format(node_id, field))
            if not values:
                missing.append("route:{}:{}".format(node_id, field))
            elif len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values}) > 1:
                missing.append("route:{}:conflict:{}".format(node_id, field))
    return missing


def diagnose_skill(name: str, report: Any, config: dict) -> SkillDiagnostic:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("skill name is required")
    skills = getattr(report, "skills", [])
    project = {item.get("name") for item in skills if item.get("valid")}
    configured = config if isinstance(config, dict) else {}
    global_skills = configured.get("global_skills", [])
    configured_skills = configured.get("skills", [])
    if name in project:
        return SkillDiagnostic(name, "ready", "project reference present")
    present_global = name in global_skills or name in configured_skills
    reason = "global Skill present; project reference missing" if present_global else "Skill is not configured"
    return SkillDiagnostic(name, "attention", reason, Proposal(True, "Configure this Skill after confirmation."))


def build_skill_reference_proposal(diagnostic: SkillDiagnostic) -> Proposal:
    if diagnostic.status != "attention":
        return Proposal(False)
    return Proposal(True, "# Skill proposal\n\n- name: {}\n- action: add project reference\n".format(diagnostic.name))


def check_agents_contract(content: str, required_rules: List[str]) -> ContractCheck:
    if not isinstance(content, str) or not isinstance(required_rules, list):
        raise TypeError("content and required_rules are required")
    missing = [rule for rule in required_rules if rule not in content]
    return ContractCheck(not missing, missing)


def build_agentsmd_proposal(check: ContractCheck) -> Proposal:
    if check.ok:
        return Proposal(False)
    return Proposal(True, "# Vibe Guide contract proposal\n\n" + "\n".join("- " + item for item in check.missing) + "\n")


def diagnose_legacy_plan(plan_dir: Any) -> LegacyPlanDiagnostic:
    """Inspect a legacy V2 plan without mutating any source artifact.

    A valid legacy identity is still not executable: it is routed to a fresh
    revision.  Contradictory or incomplete identity is ``blocked_unknown`` so
    callers cannot guess which authorization lineage should be retained.
    """
    root = Path(plan_dir)
    if root.is_symlink() or not root.is_dir():
        return LegacyPlanDiagnostic("blocked_unknown", None, ["plan-directory"], remediation="provide a real plan directory")
    plan = _read_json(root / "plan.json")
    if plan is None:
        return LegacyPlanDiagnostic("blocked_unknown", None, ["plan.json"], remediation="restore a readable plan.json with plan_id and version")
    plan_id = plan.get("plan_id", plan.get("id"))
    version = plan.get("version", plan.get("plan_version"))
    if not isinstance(plan_id, str) or not plan_id.strip() or isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return LegacyPlanDiagnostic("blocked_unknown", plan_id if isinstance(plan_id, str) else None, ["lineage"], reason="plan identity or revision is ambiguous", remediation="supply an unambiguous plan_id and positive version")
    card, card_present = _read_optional_json(root / "authorization-card.json")
    if not card_present:
        # V2 used both names across revisions; accept either as historical
        # evidence while keeping it non-authorizing for the new revision.
        card, card_present = _read_optional_json(root / "authorization.json")
    if card_present and card is None:
        return LegacyPlanDiagnostic("blocked_unknown", plan_id, ["lineage"], reason="authorization card is unreadable", remediation="restore the original card or provide a new revision")
    if card is not None:
        card_plan = card.get("plan_id", card.get("plan"))
        card_version = card.get("plan_version", card.get("version"))
        if (card_plan is not None and card_plan != plan_id) or (card_version is not None and card_version != version):
            return LegacyPlanDiagnostic("blocked_unknown", plan_id, ["lineage"], reason="authorization card does not bind the legacy plan", remediation="reconcile card plan_id/version before migration")
    missing: List[str] = []
    if card is None:
        missing.append("authorization-card")
    audit, audit_present = _read_optional_json(root / "dag-audit.json")
    if audit_present and audit is None:
        return LegacyPlanDiagnostic("blocked_unknown", plan_id, ["lineage"], reason="DAG audit is unreadable", remediation="re-audit the new revision")
    if audit is None:
        missing.append("dag-audit")
    elif audit.get("plan_id") is not None and audit.get("plan_id") != plan_id:
        return LegacyPlanDiagnostic("blocked_unknown", plan_id, ["lineage"], reason="DAG audit points at another plan", remediation="re-audit the new revision")
    confirmation, confirmation_present = _read_optional_json(root / "plan-confirmation.json")
    if confirmation_present and confirmation is None:
        return LegacyPlanDiagnostic("blocked_unknown", plan_id, ["lineage"], reason="plan confirmation is unreadable", remediation="confirm the new revision")
    if confirmation is None:
        missing.append("plan-confirmation")
    else:
        confirmation_plan = confirmation.get("plan_id", confirmation.get("plan"))
        confirmation_version = confirmation.get("plan_revision", confirmation.get("version"))
        if (confirmation_plan is not None and confirmation_plan != plan_id) or (confirmation_version is not None and confirmation_version != version):
            return LegacyPlanDiagnostic("blocked_unknown", plan_id, ["lineage"], reason="plan confirmation points at another revision", remediation="discard the stale confirmation and confirm the new revision")
        if card is not None and confirmation.get("authorization_digest") is not None and confirmation.get("authorization_digest") != card.get("digest"):
            return LegacyPlanDiagnostic("blocked_unknown", plan_id, ["lineage"], reason="confirmation digest does not match its card", remediation="do not reuse the old digest; issue a new confirmation")
    if str(plan.get("status", "draft")).lower() in {"draft", "planned", "planning_required"} and "plan-confirmation" not in missing:
        missing.append("plan-confirmation")
    missing.extend(_route_bindings(plan, root))
    route_issues = [
        item
        for item in missing
        if ":invalid" in item or ":conflict:" in item
    ]
    if route_issues:
        return LegacyPlanDiagnostic(
            "blocked_unknown",
            plan_id,
            sorted(set(missing)),
            reason="legacy route metadata is malformed or contradictory",
            remediation="reconcile route fields and provide a new revision",
        )
    # Even a complete-looking V2 directory cannot authorize a V3 run.  The
    # report records what was absent while routing to the next revision.
    return LegacyPlanDiagnostic("planning_required", plan_id, sorted(set(missing)), reason="legacy V2 evidence cannot authorize a current run", remediation="create and confirm a new revision")


def _plan_root(paths: Any, plan_id: str) -> Path:
    return paths.resolve_vibe_path(Path("plans") / plan_id)


def assert_planning_gate(paths: Any, plan_id: str, allow_reauthorization: bool = False) -> PlanningGate:
    root = _plan_root(paths, plan_id)
    required = ["prd.md", "plan.json", "nodes.json", "authorization-card.json"]
    missing = [name for name in required if not _regular(root / name)]
    if missing:
        return PlanningGate("planning_required", plan_id, sorted(set(missing)))
    try:
        prd = (root / "prd.md").read_text(encoding="utf-8")
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
        nodes = json.loads((root / "nodes.json").read_text(encoding="utf-8"))
        card = json.loads((root / "authorization-card.json").read_text(encoding="utf-8"))
        if "approved" not in prd.lower() or "review" not in prd.lower():
            missing.append("prd.reviewed")
        if plan.get("status") not in ("authorized", "running", "complete"):
            missing.append("plan.published")
        if not isinstance(nodes, list) or not nodes:
            missing.append("nodes")
        for directory, label in (("specs", "spec"), ("issues", "issue")):
            files = list((root / directory).glob("*.md")) if (root / directory).is_dir() else []
            if not files:
                missing.append(directory + ".reviewed")
            for item in files:
                text = item.read_text(encoding="utf-8")
                node_ids = {node.get("id") for node in nodes if isinstance(node, dict)}
                if item.stem not in node_ids or item.stem not in text or "published" not in text.lower() or "review" not in text.lower():
                    missing.append(label + ":" + item.name)
        audit_path = root / "dag-audit.json"
        if not _regular(audit_path):
            missing.append("dag-audit")
        else:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            node_ids = {node.get("id") for node in nodes if isinstance(node, dict)}
            if audit.get("status") != "reviewed" or audit.get("node_count") != len(nodes) or set(audit.get("node_ids", [])) != node_ids:
                missing.append("dag-audit.invalid")
        confirmation_path = root / "plan-confirmation.json"
        if not _regular(confirmation_path):
            missing.append("plan-confirmation")
        else:
            confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
            if confirmation.get("status") != "confirmed" or confirmation.get("plan_revision") not in (str(plan.get("version")), plan.get("version")) or confirmation.get("authorization_digest") != card.get("digest"):
                missing.append("plan-confirmation.invalid")
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        missing.append("published_artifacts")
    return PlanningGate("execution_ready" if not missing else "planning_required", plan_id, sorted(set(missing)))


def require_execution_ready(gate: PlanningGate) -> None:
    if gate.status != "execution_ready":
        raise PermissionError("planning_required: " + ", ".join(gate.missing))


def screen_session(paths: Any, session_id: str, request: str, origin: str = "user_entry") -> SessionGate:
    if origin not in ("user_entry", "worker_dispatch") or not session_id or not isinstance(request, str):
        raise ValueError("invalid session binding")
    directory = paths.vibe_dir
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".session-gates.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        target = directory / "session-gates.json"
        if target.is_symlink():
            raise PermissionError("session gate path is symlinked")
        try:
            records = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise PermissionError("session gate persistence failed") from error
        digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
        previous = records.get(session_id)
        if previous is not None:
            if previous.get("request_digest") != digest or previous.get("origin") != origin:
                raise PermissionError("session binding conflict")
            return SessionGate(previous.get("status", "session_screened"), session_id, request, origin)
        records[session_id] = {"status": "session_screened", "session_id": session_id, "origin": origin, "request_digest": digest, "evidence_ref": "session-gates.json#" + session_id}
        descriptor, temporary = tempfile.mkstemp(prefix=".session-gates.", dir=str(directory))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(records, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return SessionGate("session_screened", session_id, request, origin)


def require_session_screened(gate: SessionGate) -> None:
    if gate.status != "session_screened":
        raise PermissionError("session screening required")


def validate_child_session_binding(parent_run_id: str, plan_revision: str, authorization_digest: str, node_id: str, role: str, worker_profile: Any) -> None:
    if not all(isinstance(value, str) and value.strip() for value in (parent_run_id, plan_revision, authorization_digest, node_id)) or role not in ("developer", "reviewer", "rework", "successor"):
        raise ValueError("child session binding is incomplete")
    if not getattr(worker_profile, "allowlist", None) or not getattr(worker_profile, "writer", None) or not getattr(worker_profile, "worktree", None) or not getattr(worker_profile, "branch", None):
        raise ValueError("worker profile is required")
    for item in worker_profile.allowlist:
        candidate = Path(item)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("allowlist must remain within project")


planning_required_diagnostic = diagnose_legacy_plan
legacy_plan_diagnostic = diagnose_legacy_plan


__all__ = [
    "Proposal", "SkillDiagnostic", "ContractCheck", "PlanningGate", "SessionGate",
    "LegacyPlanDiagnostic", "diagnose_legacy_plan", "planning_required_diagnostic",
    "legacy_plan_diagnostic", "diagnose_skill", "build_skill_reference_proposal",
    "check_agents_contract", "build_agentsmd_proposal", "assert_planning_gate",
    "require_execution_ready", "screen_session", "require_session_screened",
    "validate_child_session_binding",
]
