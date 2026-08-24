import argparse
import json


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vibe")
    parser.add_argument("command", choices=("scan", "monitor"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.command == "monitor":
        print("authorization required")
        return 3
    result = {"command": "scan", "status": "ok"}
    print(json.dumps(result) if args.as_json else "scan: ok")
    return 0

