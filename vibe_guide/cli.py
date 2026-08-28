"""Minimal CLI for read-only Change Request capability classification."""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Sequence

from .change_requests import ChangeRequest, classify_merge_capability


def run_cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="vibe")
    parser.add_argument("command", choices=("change-request",))
    parser.add_argument("--request", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv))
    data = json.loads(Path(args.request).read_text(encoding="utf-8"))
    cr_data = data.get("change_request", data)
    observed = data.get("observed_facts", data)
    capability = classify_merge_capability(observed)
    request = ChangeRequest(
        cr_data["provider"], cr_data["kind"], cr_data["source"], cr_data["target"],
        cr_data["head_sha"], cr_data["tree_sha"], capability, cr_data.get("status", ""),
        cr_data.get("issue_id", cr_data.get("issue", "")),
        cr_data.get(
            "change_request_id",
            cr_data.get("change_request", cr_data.get("request_id", cr_data.get("mr_id", cr_data.get("pr_id", "")))),
        ),
    )
    payload: Dict[str, Any] = {
        "command": "change-request",
        "status": "blocked_unknown" if capability == "unknown_remote" else capability,
        "merge_capability": capability,
        "change_request": request.to_dict(),
        "remote_merge": capability == "verified_remote",
        "local_merge": capability in {"denied_remote", "unsupported_remote"},
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json else payload["status"])
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(run_cli(sys.argv[1:]))
