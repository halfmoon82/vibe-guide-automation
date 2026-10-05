"""Agent-facing protocol documents shipped with vibe.

vibe provides the protocol, validation and scaffolding; the host agent
provides PRD content and node decomposition.  The documents here are the
protocol half: ``vibe init`` materializes them into the project as proposals
and the CLI can print them, so a fresh session never has to hunt for them.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict

PRD_GUIDE_NAME = "prd-guide"
#: Where ``vibe init`` places the protocol inside a project.
PRD_GUIDE_PROPOSAL_RELATIVE = ".vibe/proposals/skills/prd-guide/SKILL.md"

VIBE_ENTRY_NAME = "vibe-entry"
#: The new-session entry protocol materializes next to prd-guide.
VIBE_ENTRY_PROPOSAL_RELATIVE = ".vibe/proposals/skills/vibe-entry/SKILL.md"

#: sha256 of every prd-guide / vibe-entry text vibe has ever shipped, the
#: current one included.  A project copy matching one of these is an unedited
#: earlier release and may be refreshed; any other text is a local edit and is
#: left alone.  Append the new digest whenever a protocol changes.
SHIPPED_PROTOCOL_DIGESTS = {
    "prd-guide": frozenset({
        "08591263683208c510570d6af2f3733bb0596ecd1c00bdb2dc8fc7206fabaa13",
        "0e6c7401ff1c6c6565da6a28202cc0312e7c3f5bbc3ba139a536cfaf3b11dfd0",
        "15c531ea1b21a28f8178e9c90fa7d80a4a8afec04a4de0ff624817b2d288a6ab",
        "180f67396aa65b2019572f0e0154ee2e2be1b517e6179929466f2c24280b6166",
        "1e13aa069970d089ae79444fe0b0fba4987ae1929e4cdf002cda40d5ff272f9a",
        "217b7276e296ddaaacb00ee91bf4c4608610325bd292e7809dc4cc8b021799c1",
        "3f17774b7981938c52cf6c9a58476f3b46d1c4bf931328f78d0a0b689dabbd88",
        "524c8dcc8925d635361faf81deaa405f0ceae666f979f5f1d6002a31aaf4cca3",
        "71c5130222474d5852c8233978e72f927c499d785f56948d78728acbd609a190",
        "94d652883bf2bf187793f41f1b000c097664674ec9a7de8355fe8c179e7bee65",
        "96d1c163c59b73e86a7967bc11ddf5dc6730d033ddf1b299efc0804eb3bf7e8b",
        "c2c11e2c6db35977be5df3c8ad6ea272cbba58acf0ca873794a5172ab74c08ea",
        "d31adcdf3fb33d3b6e2fd557f7324cbdbd07a80acc0a8d18f5ce186c23971ac7",
        "d93d32ba556b02d8e132bb78829e8b8eaf354e5420553908873c576597c7634e",
        "f2f90466346715fcf1b65a8f8cc3f5be6aa5244c676fe4fb38fc725c89610c38",
    }),
    "vibe-entry": frozenset({
        "08140f273188868532d5724d4fb199d1ee48ac46c80cbc2786f46fce91e4ddff",
        "2033500aac595e39e51d2bf4c42aaf757cb8ab94522b0c289587f68b15c500c2",
        "209d83d6f70e0444dce589630b95662fd369d38b7e9e3dfd7677293c6554ce24",
        "6ab2be64c1366b290c2c779a61702d881d4ad53f59b688ccae423acf7a596df3",
        "8091f9d39f66ecb373956d41cf573073420b78be173c53371e51f94e2ee356a6",
        "8a231e030151ff3a2378aaf1d1cd95745c7374e7cb95b1d4935c641d53b7287d",
        "e0a321c246cd2636a31612d643d7982c91ab5b473c80056b67cd800c853cd78e",
    }),
}

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


def is_shipped_protocol(name: str, text: str) -> bool:
    """True when ``text`` is byte-for-byte a version of ``name`` vibe shipped."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest in SHIPPED_PROTOCOL_DIGESTS.get(name, frozenset())


def protocol_schema_example(text: str) -> Dict[str, Any]:
    """Extract the JSON schema example under §5.1 so tests can pin it to the code."""
    start = text.find(_SCHEMA_HEADING)
    if start == -1:
        raise ValueError("protocol has no schema section")
    match = re.search(r"```json\n(.*?)\n```", text[start:], re.S)
    if not match:
        raise ValueError("protocol schema section has no json block")
    return json.loads(match.group(1))
