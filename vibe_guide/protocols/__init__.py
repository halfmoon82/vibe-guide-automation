"""Agent-facing protocol documents shipped with vibe.

vibe provides the protocol, validation and scaffolding; the host agent
provides PRD content and node decomposition.  The documents here are the
protocol half: ``vibe init`` materializes them into the project as proposals
and the CLI can print them, so a fresh session never has to hunt for them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

PRD_GUIDE_NAME = "prd-guide"
#: Where ``vibe init`` places the protocol inside a project.
PRD_GUIDE_PROPOSAL_RELATIVE = ".vibe/proposals/skills/prd-guide/SKILL.md"

_HERE = Path(__file__).resolve().parent
_SCHEMA_HEADING = "### 5.1"


def load_protocol(name: str) -> str:
    """Return the shipped protocol text; the name is a simple identifier."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name or ""):
        raise ValueError("protocol name must be a simple identifier")
    path = _HERE / (name + ".md")
    if not path.is_file():
        raise FileNotFoundError("unknown protocol: {}".format(name))
    return path.read_text(encoding="utf-8")


def protocol_schema_example(text: str) -> Dict[str, Any]:
    """Extract the JSON schema example under §5.1 so tests can pin it to the code."""
    start = text.find(_SCHEMA_HEADING)
    if start == -1:
        raise ValueError("protocol has no schema section")
    match = re.search(r"```json\n(.*?)\n```", text[start:], re.S)
    if not match:
        raise ValueError("protocol schema section has no json block")
    return json.loads(match.group(1))
