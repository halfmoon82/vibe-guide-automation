"""V6 P2a: Ollama 产品初始化与运行时回退。

只写安装器代码与合同；本模块默认不真实安装 Ollama、不下载模型、不改系统配置。
真实网络与安装动作通过注入的 ``fetch`` / ``post`` / ``installer`` 完成；
默认 ``fetch``/``post`` 用 urllib 走 127.0.0.1，``installer`` 是 no-op 占位。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

from . import fsio

LOCAL_MODEL_RECORD = ".vibe/installation/local_model.json"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
ENV_ENDPOINT = "VIBE_OLLAMA_ENDPOINT"
_INSTALL_SOURCE = "https://ollama.com/download"
_INSTALL_ROLLBACK_SCOPE = "仅移除 Ollama 二进制与 ~/.ollama 模型目录；不动系统服务、环境变量或项目文件"

# 运行时回退原因：超时、错误 JSON、无来源、服务繁忙。
_FALLBACK_REASONS = {"timeout", "invalid_json", "no_source", "busy", "remote_endpoint", "unreachable"}

Fetch = Callable[[str, float], Tuple[int, str]]
Post = Callable[[str, Dict[str, Any], float], Tuple[int, str]]


def _default_fetch(url: str, timeout: float) -> Tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", "replace")


def _default_post(url: str, body: Dict[str, Any], timeout: float) -> Tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", "replace")


def _resolve_endpoint(endpoint: Optional[str]) -> str:
    if endpoint:
        return endpoint.rstrip("/")
    env = os.environ.get(ENV_ENDPOINT)
    if env:
        return env.rstrip("/")
    return DEFAULT_ENDPOINT


def _is_local_endpoint(endpoint: str) -> bool:
    try:
        host = urlparse(endpoint).hostname or ""
    except ValueError:
        return False
    host = host.lower()
    if host in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        return True
    return host.startswith("127.")


def _record_path(project_root: Path | str) -> Path:
    return Path(project_root) / LOCAL_MODEL_RECORD


def _persist_state(project_root: Path | str, record: Dict[str, Any]) -> Path:
    path = _record_path(project_root)
    fsio.atomic_write_json(path, record)
    return path


def _read_record(project_root: Path | str) -> Dict[str, Any]:
    path = _record_path(project_root)
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def probe_ollama(endpoint: Optional[str] = None, fetch: Optional[Fetch] = None, timeout: float = 3.0) -> Dict[str, Any]:
    """Real probe: GET /api/tags.  HTTP 200 alone is NOT readiness — the body
    must parse as JSON with a ``models`` list.

    Returns a dict with ``status`` in {available, unavailable, unknown,
    unknown_timeout, remote_endpoint} plus ``local``/``send_project_data``.
    A remote (non-loopback) endpoint never counts as ready for project data.
    """
    ep = _resolve_endpoint(endpoint)
    local = _is_local_endpoint(ep)
    fetch = fetch or _default_fetch
    base: Dict[str, Any] = {"endpoint": ep, "local": local, "send_project_data": local}
    try:
        status, body = fetch(ep + "/api/tags", timeout)
    except TimeoutError:
        return {**base, "status": "unknown_timeout"}
    except (OSError, urllib.error.URLError, ConnectionError):
        return {**base, "status": "unavailable"}
    except Exception:
        return {**base, "status": "unknown"}
    if status != 200:
        return {**base, "status": "unavailable" if status in (0,) or status >= 500 else "unknown"}
    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return {**base, "status": "unknown"}
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return {**base, "status": "unknown"}
    models = [m.get("name") for m in payload["models"] if isinstance(m, dict) and isinstance(m.get("name"), str)]
    if not local:
        return {**base, "status": "remote_endpoint", "models": models}
    return {**base, "status": "available", "models": models}


def _install_plan(endpoint: str) -> Dict[str, Any]:
    return {
        "source": _INSTALL_SOURCE,
        "download_size": "约 500 MB 安装包 + 模型按需下载（首个模型数 GB）",
        "rollback_scope": _INSTALL_ROLLBACK_SCOPE,
        "endpoint": endpoint,
    }


def _self_check(endpoint: str, post: Post, timeout: float = 30.0) -> Dict[str, Any]:
    """Real inference self-check: POST /api/generate with a fixed ping prompt.
    Never called for remote endpoints.
    """
    try:
        status, body = post(
            endpoint + "/api/generate",
            {"model": "", "prompt": "ping", "stream": False},
            timeout,
        )
    except Exception as exc:
        return {"inference_ok": False, "error": type(exc).__name__}
    if status != 200:
        return {"inference_ok": False, "error": "http_%d" % status}
    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return {"inference_ok": False, "error": "invalid_json"}
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), str):
        return {"inference_ok": False, "error": "no_response"}
    return {"inference_ok": True}


def _default_installer(plan: Dict[str, Any]) -> Dict[str, Any]:
    # 占位安装器：P2a 不真实安装，返回未执行。真实安装由后续授权批次接入。
    return {"status": "not_executed", "reason": "installer_not_authorized"}


def init_local_model(
    project_root: Path | str,
    endpoint: Optional[str] = None,
    fetch: Optional[Fetch] = None,
    post: Optional[Post] = None,
    authorizer: Optional[Callable[[Dict[str, Any]], Any]] = None,
    installer: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """发现 → 授权 → 安装或接入 → 模型准备 → 真实推理自检。

    结果落盘到 ``.vibe/installation/local_model.json``。degraded 模式是产品
    决策（decision A），不阻塞安装完成，但如实记录原因。
    """
    ep = _resolve_endpoint(endpoint)
    post = post or _default_post
    probe = probe_ollama(ep, fetch=fetch)
    record: Dict[str, Any] = {"endpoint": ep, "probe": probe}

    if not probe["local"] or probe["status"] == "remote_endpoint":
        record.update(status="degraded", reason="remote_endpoint")
        _persist_state(project_root, record)
        return {"status": "degraded", "reason": "remote_endpoint", "probe": probe}

    if probe["status"] == "unknown_timeout":
        record.update(status="unknown", reason="probe_timeout")
        _persist_state(project_root, record)
        return {"status": "unknown", "reason": "probe_timeout", "probe": probe}

    if probe["status"] == "unknown":
        record.update(status="unknown", reason="probe_invalid")
        _persist_state(project_root, record)
        return {"status": "unknown", "reason": "probe_invalid", "probe": probe}

    if probe["status"] == "unavailable":
        plan = _install_plan(ep)
        record["plan"] = plan
        auth = authorizer(plan) if callable(authorizer) else {"status": "awaiting"}
        auth_status = auth.get("status") if isinstance(auth, dict) else str(auth)
        if auth_status == "declined":
            record.update(status="degraded", reason="declined", authorization=auth)
            _persist_state(project_root, record)
            return {"status": "degraded", "reason": "declined", "plan": plan}
        if auth_status != "approved":
            record.update(status="awaiting_authorization", authorization=auth)
            _persist_state(project_root, record)
            return {"status": "awaiting_authorization", "plan": plan}
        installer = installer or _default_installer
        outcome = installer(plan)
        outcome = outcome if isinstance(outcome, dict) else {"status": str(outcome)}
        record["install"] = outcome
        if outcome.get("status") == "interrupted":
            record.update(status="interrupted", resume_step=outcome.get("resume_step", "download"))
            _persist_state(project_root, record)
            return {"status": "interrupted", "resume_step": record["resume_step"]}
        if outcome.get("status") == "not_executed":
            record.update(status="degraded", reason=outcome.get("reason", "installer_not_authorized"))
            _persist_state(project_root, record)
            return {"status": "degraded", "reason": record["reason"], "plan": plan}
        if outcome.get("status") != "complete":
            reason = outcome.get("reason") or "install_failed"
            record.update(status="degraded" if reason != "install_failed" else "failed", reason=reason)
            _persist_state(project_root, record)
            return {"status": record["status"], "reason": reason, "plan": plan}
        # 安装完成后再探一次确认接入
        probe = probe_ollama(ep, fetch=fetch)
        record["probe"] = probe
        if probe["status"] != "available":
            record.update(status="degraded", reason="post_install_not_ready")
            _persist_state(project_root, record)
            return {"status": "degraded", "reason": "post_install_not_ready", "plan": plan}

    # 已可用（或刚装好）：真实推理自检
    check = _self_check(ep, post)
    record["self_check"] = check
    if not check["inference_ok"]:
        record.update(status="degraded", reason="selfcheck_failed")
        _persist_state(project_root, record)
        return {"status": "degraded", "reason": "selfcheck_failed", "self_check": check, "probe": probe}
    record.update(status="ready", reason=None)
    _persist_state(project_root, record)
    return {"status": "ready", "probe": probe, "self_check": check}


def resume_local_model(
    project_root: Path | str,
    endpoint: Optional[str] = None,
    fetch: Optional[Fetch] = None,
    post: Optional[Post] = None,
    authorizer: Optional[Callable[[Dict[str, Any]], Any]] = None,
    installer: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """离线或下载中断后的续接：只提示一键继续，沿用首次授权范围，不静默下载。

    本函数本身不触碰网络或安装器；返回 ``awaiting_continue`` 让调用方提示
    用户后再重新调用 ``init_local_model``（沿用首次授权范围）。
    """
    record = _read_record(project_root)
    if record.get("status") != "interrupted":
        return init_local_model(project_root, endpoint=endpoint, fetch=fetch, post=post,
                                authorizer=authorizer, installer=installer)
    return {
        "status": "awaiting_continue",
        "resume_step": record.get("resume_step", "download"),
        "scope": "original",
        "plan": record.get("plan"),
    }


def _is_remote(endpoint: Optional[str]) -> bool:
    return not _is_local_endpoint(_resolve_endpoint(endpoint))


def _deterministic_material(text: str, limit: int = 4000) -> str:
    """确定性回退材料：取原文尾部（增量通常在尾部），限长。"""
    if len(text) <= limit:
        return text
    return text[-limit:]


def summarize_with_fallback(
    text: str,
    endpoint: Optional[str] = None,
    post: Optional[Post] = None,
    model: str = "",
    timeout: float = 20.0,
    material_limit: int = 4000,
) -> Dict[str, Any]:
    """对长非结构化增量做本地压缩；故障时回退确定性材料。

    永不返回“无事/已通过”伪结果：故障一律 ``status=fallback`` 并带原因。
    端点指向远端时不发送项目资料，直接回退。
    """
    if _is_remote(endpoint):
        return {
            "status": "fallback",
            "reason": "remote_endpoint",
            "deterministic": True,
            "material": _deterministic_material(text, material_limit),
        }
    ep = _resolve_endpoint(endpoint)
    post = post or _default_post
    try:
        status, body = post(ep + "/api/generate", {"model": model, "prompt": text, "stream": False}, timeout)
    except TimeoutError:
        return {"status": "fallback", "reason": "timeout", "deterministic": True,
                "material": _deterministic_material(text, material_limit)}
    except Exception:
        return {"status": "fallback", "reason": "unreachable", "deterministic": True,
                "material": _deterministic_material(text, material_limit)}
    if status == 503 or status >= 500:
        return {"status": "fallback", "reason": "busy", "deterministic": True,
                "material": _deterministic_material(text, material_limit)}
    if status != 200:
        return {"status": "fallback", "reason": "unreachable", "deterministic": True,
                "material": _deterministic_material(text, material_limit)}
    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return {"status": "fallback", "reason": "invalid_json", "deterministic": True,
                "material": _deterministic_material(text, material_limit)}
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), str):
        return {"status": "fallback", "reason": "no_source", "deterministic": True,
                "material": _deterministic_material(text, material_limit)}
    # 模型输出只作为字符串摘要；任何指令形态字段不进入结果，不能改变权限。
    return {"status": "ok", "summary": payload["response"], "source": "ollama", "deterministic": False}


class SummaryCache:
    """摘要缓存：绑定证据 digest 与授权版本；任一变更新即失效。"""

    def __init__(self) -> None:
        self._entries: Dict[str, Dict[str, str]] = {}

    def put(self, key: str, summary: str, *, evidence_digest: str, auth_version: str) -> None:
        self._entries[key] = {
            "summary": summary,
            "evidence_digest": str(evidence_digest),
            "auth_version": str(auth_version),
        }

    def get(self, key: str, *, evidence_digest: str, auth_version: str) -> Optional[str]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry["evidence_digest"] != str(evidence_digest) or entry["auth_version"] != str(auth_version):
            return None
        return entry["summary"]


def install_probe(request: Any = None, fetch: Optional[Fetch] = None) -> Dict[str, Any]:
    """``run_install``/``run_upgrade`` 的真实探针：返回 Ollama 探测结果。

    带 ``probed=True`` 区分旧的空探针（默认 ``{"status": "verified"}``），
    避免“没探测却报已验证”。status 恒为 verified 是因为降级是产品决策，
    但 ``local_model.status`` 如实记录，供 doctor/降级展示。
    """
    probe = probe_ollama(fetch=fetch)
    return {
        "status": "verified",
        "probed": True,
        "local_model": probe,
    }


__all__ = [
    "LOCAL_MODEL_RECORD",
    "SummaryCache",
    "init_local_model",
    "install_probe",
    "probe_ollama",
    "resume_local_model",
    "summarize_with_fallback",
]
