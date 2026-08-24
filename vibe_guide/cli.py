"""Thin public CLI dispatch and stable exit-code mapping."""

import argparse
import json
from typing import Optional, Sequence


SUCCESS = 0
USAGE_ERROR = 2
BLOCKED = 3
UNKNOWN = 4


def _emit(payload, as_json: bool, human: str) -> None:
    print(json.dumps(payload, sort_keys=True) if as_json else human)


def handle_monitor(as_json: bool = False, authorization: Optional[str] = None, runner=None) -> int:
    """Fail closed before authorization; never start a runner on that path."""
    if not authorization:
        payload = {"command": "monitor", "status": "blocked", "reason": "authorization required"}
        _emit(payload, as_json, "monitor: authorization required")
        return BLOCKED
    payload = {"command": "monitor", "status": "unknown", "reason": "runtime state unavailable"}
    _emit(payload, as_json, "monitor: runtime state unknown")
    return UNKNOWN


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe")
    parser.add_argument(
        "command",
        choices=("scan", "init", "doctor", "plan", "monitor", "status", "resume"),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--authorization-token", dest="authorization", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None, runner=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)

    if args.command == "scan":
        _emit({"command": "scan", "status": "ok"}, args.as_json, "scan: ok")
        return SUCCESS
    if args.command == "monitor":
        return handle_monitor(args.as_json, args.authorization, runner)
    if args.command == "init":
        _emit({"command": "init", "status": "blocked", "reason": "confirmation required"}, args.as_json, "init: confirmation required")
        return BLOCKED
    payload = {"command": args.command, "status": "unknown", "reason": "runtime state unavailable"}
    _emit(payload, args.as_json, "%s: runtime state unknown" % args.command)
    return UNKNOWN
