"""Read-only Change Request boundary exposed to the monitor."""

from typing import Any, Dict, Mapping

from .change_requests import ChangeRequest, classify_merge_capability, merge_local, merge_remote
from .authorization import is_action_authorized, require_action_authorized


class Monitor:
    @staticmethod
    def classify_change_request(observed_facts: Mapping[str, Any]) -> Dict[str, Any]:
        capability = classify_merge_capability(observed_facts)
        return {
            "status": "blocked_unknown" if capability == "unknown_remote" else capability,
            "merge_capability": capability,
            "remote_merge": capability == "verified_remote",
            "local_merge": capability in {"denied_remote", "unsupported_remote"},
        }

    @staticmethod
    def merge_local(change_request: ChangeRequest, authorization: Any, local_facts: Mapping[str, Any] = None) -> Dict[str, Any]:
        return merge_local(change_request, authorization, local_facts)

    @staticmethod
    def merge_remote(change_request: ChangeRequest, authorization: Any, remote_facts: Mapping[str, Any] = None) -> Dict[str, Any]:
        return merge_remote(change_request, authorization, remote_facts)

    @staticmethod
    def is_action_authorized(authorization: Any, action: str, merge_scope: Mapping[str, Any] = None) -> bool:
        return is_action_authorized(authorization, action, merge_scope)

    @staticmethod
    def require_action_authorized(authorization: Any, action: str, merge_scope: Mapping[str, Any] = None) -> None:
        require_action_authorized(authorization, action, merge_scope)
