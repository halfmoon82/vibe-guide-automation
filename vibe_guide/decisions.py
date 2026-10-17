"""Durable, option-bound decision request and answer records."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import re
from typing import Any, Dict, Iterable, Mapping, Optional


SCHEMA_VERSION = 1
_MAX_RECORD_BYTES = 64 * 1024
_DECISION_ID = re.compile(r"^dec-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: Holds and decisions share two canonical boundary categories; the older
#: names stay accepted as aliases and are always stored canonical.
BOUNDARY_CATEGORIES = ("product_acceptance", "external_authorization")
BOUNDARY_CATEGORY_ALIASES = {
    "product_design": "product_acceptance",
    "deploy": "external_authorization",
    # The four human-interrupt cases: product scope/design change (above),
    # deploy/release (above), credentials/system permission and irreversible
    # actions outside the authorization card.
    "credentials": "external_authorization",
    "system_permission": "external_authorization",
    "irreversible_action": "external_authorization",
}
#: Records written before categories existed read as this.
LEGACY_BOUNDARY_CATEGORY = "product_acceptance"


def validate_boundary_category(category):
    """Return the canonical boundary category or fail before any state write."""
    value = category.strip() if isinstance(category, str) else ""
    value = BOUNDARY_CATEGORY_ALIASES.get(value, value)
    if value not in BOUNDARY_CATEGORIES:
        raise ValueError(
            "category must be one of: {} (aliases: {}); only product scope/design "
            "changes, deploy/release, credentials/system permissions and irreversible "
            "actions outside the authorization card may hold or raise a decision. "
            "Anything else is not a human decision: 自愈——the supervisor decides it, "
            "records the correction and continues (engineering retries, cursor or "
            "evidence mismatches, structural pending-verification items)"
            .format(
                ", ".join(BOUNDARY_CATEGORIES),
                ", ".join("{}→{}".format(old, new) for old, new in BOUNDARY_CATEGORY_ALIASES.items()),
            )
        )
    return value


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("decision path may not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_RECORD_BYTES:
        raise ValueError("decision record exceeds the size bound")
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("decision record must be a regular file")
    raw = path.read_bytes()
    if len(raw) > _MAX_RECORD_BYTES:
        raise ValueError("decision record exceeds the size bound")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("decision record must be an object")
    return value


def normalize_options(options: Iterable[Any]) -> list[Dict[str, str]]:
    if isinstance(options, (str, bytes)):
        raise ValueError("decision options must be a list")
    normalized = []
    for index, option in enumerate(options):
        if isinstance(option, str):
            option_id = option.strip()
            item = {"id": option_id, "label": option, "impact": ""}
        elif isinstance(option, Mapping):
            option_id = str(option.get("id", option.get("key", ""))).strip()
            item = {
                "id": option_id,
                "label": str(option.get("label", option_id)),
                "impact": str(option.get("impact", "")),
            }
        else:
            raise ValueError("decision option {} must be a string or object".format(index))
        if not item["id"] or not item["label"]:
            raise ValueError("decision options require id and label")
        normalized.append(item)
    if len(normalized) < 2 or len({item["id"] for item in normalized}) != len(normalized):
        raise ValueError("decision requires at least two unique options")
    return normalized


class DecisionMailbox:
    """Project-contained decision mailbox shared by worker and supervisor paths."""

    def __init__(self, paths):
        self.paths = paths
        self.root = paths.resolve_vibe_path("decisions")
        self.requests = self.root / "requests"
        self.results = self.root / "results"

    def _request_path(self, decision_id: str) -> Path:
        if not isinstance(decision_id, str) or not _DECISION_ID.fullmatch(decision_id):
            raise ValueError("decision id is invalid")
        return self.requests / (decision_id + ".json")

    def _result_path(self, decision_id: str) -> Path:
        self._request_path(decision_id)
        return self.results / (decision_id + ".json")

    @staticmethod
    def _request_digest(record: Mapping[str, Any]) -> str:
        unsigned = {
            key: value
            for key, value in record.items()
            if key not in {"request_digest", "answer_cli"}
        }
        return canonical_digest(unsigned)

    def _build_record(
        self,
        *,
        run_id: str,
        node_id: str,
        question: str,
        options: Iterable[Any],
        recommendation: Optional[str] = None,
        evidence: Optional[Mapping[str, Any]] = None,
        asked_at: Optional[float] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("run_id and node_id are required")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("decision question is required")
        normalized = normalize_options(options)
        if recommendation is not None and recommendation not in {item["id"] for item in normalized}:
            raise ValueError("recommendation must be one of the options")
        canonical_category = (
            LEGACY_BOUNDARY_CATEGORY if category is None else validate_boundary_category(category)
        )
        decision_id = "dec-" + canonical_digest(
            {"run_id": run_id, "node_id": node_id, "question": question}
        )[:24]
        self._request_path(decision_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "decision_id": decision_id,
            "run_id": run_id,
            "node_id": node_id,
            "question": question,
            "category": canonical_category,
            "options": normalized,
            "recommendation": recommendation,
            "evidence": dict(evidence or {}),
            "asked_at": time.time() if asked_at is None else asked_at,
        }

    def raise_decision(self, **kwargs) -> Dict[str, Any]:
        return self._store_record(self._build_record(**kwargs))

    def _store_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Write a validated record; identical content is idempotent."""
        decision_id = record["decision_id"]
        path = self._request_path(decision_id)
        if path.exists():
            existing = self._load_request(decision_id)
            if all(existing.get(key) == record[key] for key in ("options", "recommendation", "category")):
                return existing
            # Changed content supersedes the request: the old answer is kept
            # only as archived evidence and the decision is pending again.
            self._archive_result(decision_id, existing)
        record["request_digest"] = self._request_digest(record)
        record["answer_cli"] = "vibe decision-answer --id {} --choice <option_id> --digest {}".format(decision_id, record["request_digest"])
        _atomic_json(path, record)
        return self._presented(record)

    def _archive_result(self, decision_id: str, old_request: Mapping[str, Any]) -> None:
        result_path = self._result_path(decision_id)
        if result_path.is_symlink():
            raise ValueError("decision result must be a regular file")
        if not result_path.exists():
            return
        archive = self.results / "superseded"
        if archive.is_symlink():
            raise ValueError("decision path may not be a symlink")
        archive.mkdir(parents=True, exist_ok=True)
        os.replace(
            str(result_path),
            str(archive / "{}-{}.json".format(decision_id, str(old_request.get("request_digest"))[:16])),
        )

    @staticmethod
    def _presented(record: Mapping[str, Any]) -> Dict[str, Any]:
        """Return a copy that always carries a category (legacy default)."""
        shown = dict(record)
        shown.setdefault("category", LEGACY_BOUNDARY_CATEGORY)
        return shown

    def ingest_worker_requests(self, run_id: str, node_id: str, requests: Iterable[Mapping[str, Any]]) -> list[Dict[str, Any]]:
        if not isinstance(requests, Iterable) or isinstance(requests, (str, bytes, Mapping)):
            raise ValueError("decision_requests must be a list")
        # All-or-nothing: validate and build every record before any write.
        built = []
        for item in requests:
            if not isinstance(item, Mapping):
                raise ValueError("decision request must be an object")
            built.append(self._build_record(
                run_id=run_id,
                node_id=node_id,
                question=item.get("question", ""),
                options=item.get("options", ()),
                recommendation=item.get("recommendation"),
                evidence=item.get("evidence") or item.get("evidence_binding") or {},
                category=item.get("category"),
            ))
        return [self._store_record(record) for record in built]

    def _load_request(self, decision_id: str) -> Dict[str, Any]:
        record = _read_json(self._request_path(decision_id))
        if record.get("schema_version") != SCHEMA_VERSION or record.get("decision_id") != decision_id:
            raise ValueError("decision request schema is invalid")
        if record.get("request_digest") != self._request_digest(record):
            raise ValueError("decision request digest does not match its options")
        normalize_options(record.get("options", ()))
        return self._presented(record)

    def _load_result(self, decision_id: str, request: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        request = request or self._load_request(decision_id)
        result = _read_json(self._result_path(decision_id))
        if result.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("decision result schema is invalid")
        if result.get("decision_id") != decision_id:
            raise ValueError("decision result id does not match request")
        if result.get("request_digest") != request.get("request_digest"):
            raise ValueError("decision result digest does not match request")
        options = {item["id"] for item in normalize_options(request.get("options", ()))}
        if not isinstance(result.get("choice"), str) or result["choice"] not in options:
            raise ValueError("decision result choice is not one of the current options")
        answered_at = result.get("answered_at")
        if isinstance(answered_at, bool) or not isinstance(answered_at, (int, float)) or not math.isfinite(answered_at):
            raise ValueError("decision result answered_at is invalid")
        if not isinstance(result.get("answered_via"), str) or not result["answered_via"].strip():
            raise ValueError("decision result answered_via is invalid")
        if "note" in result and not isinstance(result["note"], str):
            raise ValueError("decision result note is invalid")
        return result

    def list_pending(self, run_id: Optional[str] = None) -> list[Dict[str, Any]]:
        if self.requests.is_symlink() or self.results.is_symlink():
            raise ValueError("decision mailbox path may not be a symlink")
        if not self.requests.is_dir():
            return []
        result = []
        for path in sorted(self.requests.glob("dec-*.json")):
            record = self._load_request(path.stem)
            if run_id is not None and record.get("run_id") != run_id:
                continue
            result_path = self._result_path(path.stem)
            if result_path.is_symlink():
                raise ValueError("decision result must be a regular file")
            if result_path.exists():
                self._load_result(path.stem, record)
            else:
                result.append(record)
        return result

    def answer(
        self,
        decision_id: str,
        choice: str,
        *,
        request_digest: Optional[str] = None,
        answered_via: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(request_digest, str) or not request_digest.strip():
            raise ValueError("decision answer requires --digest of the current request")
        record = self._load_request(decision_id)
        if request_digest != record["request_digest"]:
            raise ValueError("decision request digest is stale")
        if self._result_path(decision_id).exists():
            existing = self._load_result(decision_id, record)
            if existing.get("choice") == choice:
                return existing
            raise ValueError("decision already has a different answer")
        choices = {item["id"] for item in normalize_options(record["options"])}
        if choice not in choices:
            raise ValueError("choice is not one of the current options")
        result = {
            "schema_version": SCHEMA_VERSION,
            "decision_id": decision_id,
            "request_digest": record["request_digest"],
            "choice": choice,
            "answered_at": time.time(),
            "answered_via": str(answered_via or "cli"),
        }
        if note is not None:
            result["note"] = str(note)
        _atomic_json(self._result_path(decision_id), result)
        return result

    def result(self, decision_id: str) -> Optional[Dict[str, Any]]:
        path = self._result_path(decision_id)
        return self._load_result(decision_id) if path.exists() else None

    def render_pending(self, run_id: Optional[str] = None) -> list[str]:
        return [
            "{}：[{}] {}（选项：{}；已等待未知时长）".format(
                item["decision_id"], item["category"], item["question"], ", ".join(option["id"] for option in item["options"])
            )
            for item in self.list_pending(run_id)
        ]


# Small functional aliases keep the mailbox usable by adapters without making
# them depend on its storage layout.
def create_decision_request(paths, **kwargs):
    return DecisionMailbox(paths).raise_decision(**kwargs)


def answer_request(paths, decision_id, choice, **kwargs):
    return DecisionMailbox(paths).answer(decision_id, choice, **kwargs)


def list_pending_decisions(paths, run_id=None):
    return DecisionMailbox(paths).list_pending(run_id)
