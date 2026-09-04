"""V3.10 install/upgrade contracts and a small recoverable state machine."""

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, Optional

from .models import InstallRequest, InstallResult

PHASES = ("preflight", "probe", "authorize", "backup", "migrate", "finalize")
_STATUSES = {"complete", "blocked_unknown", "blocked_invalid", "retry_pending", "failed"}

# These are provider-managed Codex skills, not Python package dependencies.
# Installation records the requirement and its unknown verification state;
# it never claims that an external skill was downloaded or activated.
SDD_SKILL_DEPENDENCIES = (
    {"name": "subagent-driven-development", "required": True, "provider": "provider-managed", "status": "unknown"},
    {"name": "test-driven-development", "required": True, "provider": "provider-managed", "status": "unknown"},
    {"name": "writing-plans", "required": True, "provider": "provider-managed", "status": "unknown"},
    {"name": "verification-before-completion", "required": True, "provider": "provider-managed", "status": "unknown"},
)


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("installation persistence path may not be a symlink")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _call(callback: Optional[Callable[..., Any]], *args: Any) -> Any:
    if callback is None:
        return {"status": "verified"}
    if callable(callback):
        return callback(*args)
    for name in ("authorize", "probe", "run", "__call__"):
        method = getattr(callback, name, None)
        if callable(method):
            return method(*args)
    raise TypeError("callback must be callable")


def _status(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("status"), str):
        return value["status"]
    return "verified"


def _version(root: Path) -> str:
    config = root / ".vibe" / "config.json"
    if not config.is_file() or config.is_symlink():
        return "unknown"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    value = data.get("version") or data.get("workflow_version") or data.get("package_version")
    return str(value) if value is not None else "unknown"


def _required_skills():
    return [dict(item) for item in SDD_SKILL_DEPENDENCIES]


def _persist_v4_config(root: Path, required_skills):
    vibe = root / ".vibe"
    if vibe.exists() and (vibe.is_symlink() or not vibe.is_dir()):
        raise ValueError(".vibe must be a regular directory")
    vibe.mkdir(parents=True, exist_ok=True)
    config = vibe / "config.json"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise ValueError(".vibe/config.json must be a regular file")
    current = {}
    if config.exists():
        try:
            loaded = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(".vibe/config.json is invalid") from error
        if not isinstance(loaded, dict):
            raise ValueError(".vibe/config.json must be an object")
        current.update(loaded)
    current.update({
        "version": "4.0.0",
        "workflow_version": 4,
        "execution_mode": "sdd_first",
        "required_skills": required_skills,
    })
    _atomic_json(config, current)


def _run(request: InstallRequest, paths: Any, capability_authorizer: Any, probe: Any, upgrade: bool) -> InstallResult:
    root = Path(request.project_root)
    before = _version(root) if upgrade else "none"
    after = "4.0.0"
    required_skills = _required_skills()
    state_path = root / ".vibe" / "installation" / "state.json"
    payload: Dict[str, Any] = {"status": "running", "phase": "preflight", "phase_history": ["preflight"], "version_before": before, "version_after": after, "required_skills": required_skills}
    _atomic_json(state_path, payload)
    capabilities: Dict[str, Any] = {}
    migration: Dict[str, Any] = {}
    backup: Dict[str, Any] = {}
    evidence = []
    try:
        probe_value = _call(probe, request)
        capabilities = probe_value if isinstance(probe_value, dict) else {"result": probe_value}
        probe_status = _status(probe_value)
        if probe_status in ("unknown_timeout", "timeout"):
            return _finish(state_path, InstallResult("retry_pending", "blocked", before, after, capabilities=capabilities, errors=["capability probe timed out"], required_skills=required_skills))
        if probe_status in ("unknown", "blocked_unknown"):
            return _finish(state_path, InstallResult("blocked_unknown", "blocked", before, after, capabilities=capabilities, errors=["capability probe is unknown"], required_skills=required_skills))
        payload = _transition(payload, "probe", capabilities=capabilities)
        _atomic_json(state_path, payload)
        auth_value = _call(capability_authorizer, request, capabilities)
        auth_status = _status(auth_value)
        if auth_status not in ("verified", "authorized", "approved", "complete", "ok"):
            return _finish(state_path, InstallResult("blocked_invalid", "blocked", before, after, capabilities=capabilities, errors=["capability authorization was not approved"], required_skills=required_skills))
        payload = _transition(payload, "authorize", capabilities=capabilities)
        _atomic_json(state_path, payload)
        if upgrade:
            payload = _transition(payload, "backup", capabilities=capabilities)
            _atomic_json(state_path, payload)
            backup = {"status": "pending"}
            payload = _transition(payload, "migrate", capabilities=capabilities, backup=backup)
            _atomic_json(state_path, payload)
            migration = {"status": "pending", "source_version": before, "target_version": after}
        _persist_v4_config(root, required_skills)
        payload = _transition(payload, "finalize", capabilities=capabilities, backup=backup, migration=migration)
        _atomic_json(state_path, payload)
        return _finish(state_path, InstallResult("complete", "complete", before, after, capabilities, migration, backup, evidence_refs=evidence, required_skills=required_skills))
    except ValueError as error:
        return _finish(state_path, InstallResult("blocked_invalid", "blocked", before, after, capabilities, migration, backup, [str(error)], evidence, required_skills=required_skills))
    except Exception as error:  # provider/runtime failures stay explicit and recoverable
        return _finish(state_path, InstallResult("failed", "blocked", before, after, capabilities, migration, backup, [str(error)], evidence, required_skills=required_skills))


def _finish(path: Path, result: InstallResult) -> InstallResult:
    final = result.to_dict()
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        existing = {}
    if isinstance(existing, dict) and "phase_history" in existing:
        final["phase_history"] = existing["phase_history"]
    _atomic_json(path, final)
    return result


def _transition(payload: Dict[str, Any], phase: str, **extra: Any) -> Dict[str, Any]:
    history = list(payload.get("phase_history", []))
    if phase not in history:
        history.append(phase)
    updated = dict(payload)
    updated.update(extra)
    updated["phase"] = phase
    updated["phase_history"] = history
    return updated


class InstallStateMachine:
    """Provider-neutral install state machine.

    Installation records the capability phases for later supervision, but does
    not probe or require a visible Agent provider.  Provider qualification is
    deliberately owned by the subsequent capability/monitor nodes.
    """

    def run(self, mode: str, target: Any) -> InstallResult:
        if mode not in ("layered", "bundled"):
            raise ValueError("unsupported installation mode")
        root = Path(target).expanduser().resolve(strict=False)
        target_text = str(root)
        required_skills = _required_skills()
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            return InstallResult("blocked_invalid", "blocked", "unknown", "4.0.0",
                                 errors=["installation target must be a directory"],
                                 mode=mode, target=target_text, required_skills=required_skills,
                                 error="installation target must be a directory")
        state_path = root / ".vibe" / "installation" / "state.json"
        payload: Dict[str, Any] = {
            "status": "running", "phase": PHASES[0],
            "phase_history": [PHASES[0]], "mode": mode, "target": target_text,
            "version_before": "unknown", "version_after": "4.0.0", "required_skills": required_skills,
        }
        try:
            # Include target preparation in the same recoverable error boundary
            # as persistence and phase execution.
            root.mkdir(parents=True, exist_ok=True)
            _atomic_json(state_path, payload)
            _persist_v4_config(root, required_skills)
            evidence = ["install:preflight"]
            payload = _transition(payload, "probe", capabilities={"status": "not_required"})
            evidence.append("install:probe:not_required")
            _atomic_json(state_path, payload)
            payload = _transition(payload, "authorize", authorization={"status": "not_required"})
            evidence.append("install:authorize:not_required")
            _atomic_json(state_path, payload)
            for phase in ("backup", "migrate", "finalize"):
                payload = _transition(payload, phase)
                _atomic_json(state_path, payload)
            result = InstallResult("complete", "complete", "unknown", "4.0.0",
                                   evidence_refs=evidence, mode=mode, target=target_text,
                                   required_skills=required_skills)
            return _finish(state_path, result)
        except (OSError, ValueError) as exc:
            result = InstallResult("blocked_invalid", "blocked", "unknown", "4.0.0",
                                   errors=[str(exc)], evidence_refs=["install:preflight"],
                                   mode=mode, target=target_text, error=str(exc), required_skills=required_skills)
            try:
                return _finish(state_path, result)
            except Exception:
                return result
        except Exception as exc:
            result = InstallResult("failed", "blocked", "unknown", "4.0.0",
                                   errors=[str(exc)], evidence_refs=["install:preflight"],
                                   mode=mode, target=target_text, error=str(exc), required_skills=required_skills)
            try:
                return _finish(state_path, result)
            except Exception:
                return result


def run_install(request: InstallRequest, paths: Any, capability_authorizer: Any = None, probe: Any = None) -> InstallResult:
    return _run(request, paths, capability_authorizer, probe, False)


def run_upgrade(request: InstallRequest, paths: Any, capability_authorizer: Any = None, probe: Any = None) -> InstallResult:
    return _run(request, paths, capability_authorizer, probe, True)


__all__ = ["InstallRequest", "InstallResult", "InstallStateMachine", "PHASES", "SDD_SKILL_DEPENDENCIES", "run_install", "run_upgrade"]
