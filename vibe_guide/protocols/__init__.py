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
        # 2026-10-26: §6.5 single shift is the default; successors must be fresh
        # sessions (fork / >=100k start refused at register); unwritable .vibe
        # stops instead of retrying (run-63e7cfc2).
        "01ca786432c5079da2685787986c342a551393208f79ff470814ca5b69f5d8a3",
        # same day, first wording of the above (committed in 2a31b1b).
        "0cbbd0e63a258cb0592e5f001b8d0dee0a0639752a2a762e463eddac98249307",
        # 2026-10-26: §6.4/§6.5 WorkBuddy 定案——Jobs detached agent 拿不到
        # 模型 run identity（凭证边界为宿主设计），派发只能走窗口会话；
        # 无自动换班入口，rotate 只能由用户在本项目 UI 新开会话接班。
        "58e05735b62294ab619c005ae56dc0e40622a374c3f0fb9861d7fdc57b570938",
        # 2026-10-26: #265 write-scope overlap rule merged onto V5.1.3's
        # 500k/800k rotate hard-cap options (run-43e778 integration, #292).
        "5e43f257106a86cfebb5c097656a39cbbe216ae717c807d9ce85a5777ec90384",
        # 2026-10-26: V5.1.3 shipped the 500k/800k rotate hard-cap options
        # without registering its text (#291); registered here so projects
        # initialized on 5.1.3 still refresh.
        "c2b86f9f3ca04c0a55de6c5c2398ecc94c81c4f292b9a61fc12951ccb39eca6f",
        # 2026-10-24: §4 write-scope overlap rule — depends_on serializes;
        # integration_after and parallel_group cannot lift an overlap (#265).
        "5e262e2c569f89b0838ce29bc2fb6b790f78cd15f6cdd52ff834cfc249509d44",
        # 2026-10-24: §6.5 rotate requires the new session be created inside the
        # project (Codex Desktop: create_thread project target; projectless
        # cwd lands outside the sandbox profile and every .vibe/ access prompts,
        # #231); supervisor-register now warns on such a cwd.
        "35277bfc6da0d71088b05a15c29e9041360cde5d9e3916121f6e94761e0ac1d7",
        # 2026-10-23: top-of-file reading map; §5.3 forced 30-minute choice with
        # cost; §6.1 supervisor-mailbox summary view; §6.4/§6.5 Claude Code
        # SendMessage wake channel + contract_drift heartbeat branch (#216④).
        "54423a04770c2cd3fa34f9e2cbf320d2c0230460a8ebec5e8dd0264686d2e15b",
        # 2026-10-17: §5.3 documents the two card choices (idle heartbeat base,
        # rotation hard cap), their availability rules and the republish rule;
        # §6 reads the cadence from the card instead of promising an automatic 30 (#209).
        "4ef389e8a32feee47c4d43fb5add9ec3b591e5a726e2621690833f4660040a3c",
        # 2026-10-17: §4/§6.2/§7 separate the product scope from the engineering
        # file list, name both auto-expansion criteria and the claim payload (#145).
        "12cacd7ba5c1bbb825831c0c7c6d09408d8fbc9063f0f6edd0bcdade3a9fe126",
        # 2026-10-17: narrower pending-verification whitelist + status listing.
        "cb73d99b1241b4d05580be33a6bce4a2b8f62674e064b390d68a85ae8bc0db8f",
        # 2026-10-17: structural pending-verification accepts only fixed sentence templates.
        "14a0edcdd465c0d1e69d1a4719d2075aa3d9f6e0a3b8289981b4fbaf3984b1b9",
        # 2026-10-17: §6.5 idle heartbeat back-off wired (pending-verification rework).
        "4bd5a46dfefe563bc9b10b3165620cfab387447cf91bc27d4746bbd0a9543acf",
        # 2026-10-17: pending-verification node — four human-interrupt cases,
        # structural pending-verification items (§6.5/§7).
        "114272b960006cddc98ac69ea105a6abfb3734e43314d244e8cc0b6fe5a43921",
        # 2026-10-16: run-9edb07bb integration merge (5 nodes on top of #210).
        "4bcee2236f297a664d8c8aa087590856298433546e1a7a61e56dc33b0418ceea",
        # intermediate text of that merge (main + heartbeat-contract only).
        "db6afb43acad8ccec154be3ed60f6b0a9f8e222ef4ced2b23a3411a897e2ed48",
        "12a69ed8e93c57af77fa531a2749fb8b479280bbc1891e4ae90e769926ba579e",
        # 2026-10-13: N4 hold/decision categories + references (#151).
        "f8021c6c2f0ba4c78e90c91706c70f2faac47f7d6cb8eede909ba2c54ae98134",
        "9223364689e4ccf6bcd1720dfa58a47d730ced15cb8b304a5074f6bd074e799e",
        "f8bccb63f6ba536f26116211c5efb9c7838c46cc83f150e1c72d742ac5f7b5c3",
        "65abc91f34211959e4917b17b0a979c5ce961f658b5dff4fcec939663dad609f",
        "06e6c3cc275851e32127d378d6dcf3cec40dceafa3f8fe3c84058d528932ba93",
        "08591263683208c510570d6af2f3733bb0596ecd1c00bdb2dc8fc7206fabaa13",
        "0e6c7401ff1c6c6565da6a28202cc0312e7c3f5bbc3ba139a536cfaf3b11dfd0",
        "15c531ea1b21a28f8178e9c90fa7d80a4a8afec04a4de0ff624817b2d288a6ab",
        "180f67396aa65b2019572f0e0154ee2e2be1b517e6179929466f2c24280b6166",
        "1e13aa069970d089ae79444fe0b0fba4987ae1929e4cdf002cda40d5ff272f9a",
        "217b7276e296ddaaacb00ee91bf4c4608610325bd292e7809dc4cc8b021799c1",
        "22d1ac5a2f9668afd90580b5444e886cbff89e51a1cf77e9754a96f191e6558e",
        "2526b81e2dc94cc865a6b47b5dd4ee933f922c627ee737531aaeaa8dd4aad6b2",
        "3f17774b7981938c52cf6c9a58476f3b46d1c4bf931328f78d0a0b689dabbd88",
        "524c8dcc8925d635361faf81deaa405f0ceae666f979f5f1d6002a31aaf4cca3",
        "61cb93abacd89626877d47f1c83fdeafe7f683e280aa4012ed64e88824f947fb",
        "71c5130222474d5852c8233978e72f927c499d785f56948d78728acbd609a190",
        "94d652883bf2bf187793f41f1b000c097664674ec9a7de8355fe8c179e7bee65",
        "96d1c163c59b73e86a7967bc11ddf5dc6730d033ddf1b299efc0804eb3bf7e8b",
        # 2026-10-08: §6.0 host differences now name WorkBuddy too.
        "9afc9c94643cec8c79f097a7278abd902700aa4c51230afff9cc087e5f2a63a8",
        "bba7d6c7c6976b5713d170f48062e18a81ea02a43071cc36850023b379e2b40c",
        "c2c11e2c6db35977be5df3c8ad6ea272cbba58acf0ca873794a5172ab74c08ea",
        "d31adcdf3fb33d3b6e2fd557f7324cbdbd07a80acc0a8d18f5ce186c23971ac7",
        # 2026-10-09: §6.3 says how WorkBuddy (no cwd parameter) pins a worktree.
        "d8dfb3a043d330928b0798e4614b671eaf885aeb37d7ef7f7923e39091ec3e3f",
        # 2026-10-09: §6.3 worker-deliver exception; serial nodes still pin.
        "2f0c8e32a2efc532df33c4392901d3a6712e4df32ac9b073d86f18c2b0d5019d",
        # 2026-10-09: #172 squash on top of #171 -- both edits in one text.
        "52ab18b4f02bbe752e6e3c1949cb6b5a98facd5299e0fbe4123649ca4ef73c7d",
        # 2026-10-13: supervisor_single_shift switch (PRD #177 D1).
        "acabc77986f59c191acdb8a4cbd7876f26f0a17d8400bc30d5b3020b6f5785ad",
        # 2026-10-13: absolute hard cap 150k -> 200k.
        "1348b17f5bfe692c5f623bc1637f55926e396658057cfd08b6ad1add8fe6c5e8",
        "d93d32ba556b02d8e132bb78829e8b8eaf354e5420553908873c576597c7634e",
        "e93c430fa34d2db44ba7dd6383cc61c6bd013a2a536642f13f6c976850e26b26",
        "f2f90466346715fcf1b65a8f8cc3f5be6aa5244c676fe4fb38fc725c89610c38",
        # 2026-10-09: heartbeat native-action consumer and stale reconciliation.
        "e06e774bdd5082ce063b6782ef15862be5a03aaa0419d1cc8ab983fddd181c07",
        # 2026-10-10: #156 single-line supervisor beat command.
        "62d464113310f1fb32a7fd3aeba880ae9d8034327fb05966ab6abb09faeb3777",
        "26f3c0f75baab3d6928dac25e86f1bab2d26828db401630ba0ce9fded393d72f",
        "7f5312b7dd47089720d54fbb22dd66e48602977c320b86ea6d3cdf6adbd794a4",
        "ffc1eb7655a839357a602ff708ae203fca2406f74a1919180530f742bf7583f1",
        "aaf782d07cc1a1c60e762a983d6fcac7143f67082ecffb7674d794ba58376e34",
        "f204123e31fa2f8ea67033dc50cba547cda8e862245fcf5e0475b8cb4b36f29e",
        "870acaa688f538bd8d09cbeba77442780a17fcb56696d79f79028640b2e1e8dd",
        "8335aee5c16b30fef9de2f1ed009df58b43f97bee4716dc09aa3dfc623b7509e",
        # 2026-10-13: native closeout integration after main reconciliation.
        "cdae7db3e3ebfcacc6db84453d9a903ae5376e1aa2134ce7d9fc838052b46708",
        # 2026-10-14: §6.4 Claude Code background (claude-code-background) column.
        "4a5b7b344ab688db332cec606350800ee25b4c98ccafd00e1c0e68b626fb2f44",
        # 2026-10-14: §6.4 resume wording and attest prerequisite.
        "b72a2d622269183724af0c7e3198f88779549ca5f009f20a322b3aca03dae687",
        # 2026-10-14: §6.1/§7 unresolved native tool: retry once, then stop, no handoff.
        "9179bef46af413f27c0954f265777778f42af5a93d6012b0b5dd83e585ab0b08",
        # 2026-10-14: §6.2 worker-deliver self-report needs no cursor (receipt instead).
        "8e6b6b2dadfc9b5e3cc6396189f17c169e434c8cd023f002055f2f9849474bee",
        # 2026-10-14: heartbeat --accept-decision + N1 intervals.
        "85c507fb3e0788f310b5da4987eefd88fbcef549de51865ad9b2491c301cba40",
        # 2026-10-16: heartbeat-contract merged onto main (#211 §6.x patches).
        "578fd47330714a53c2de616aafff3a0a9604a79ff272374aa50d2baa58a5bb35",
        # 2026-10-16: combined text once decision-boundary (#184) and heartbeat-contract both land.
        "5b7aa976a0f9b2ac20ca014a38a101635d274bad40d1a57bc4920b179ed5f38e",
        # 2026-10-17: answered decisions are consumed by the run (heartbeat-wake, §6.5).
        "fc271522236eeb9a943c59b077547bab251ce5ff33a2997954ebda4433d01612",
        # 2026-10-18: heartbeat-wake (answered decisions consumed) combined with main's idle backoff (#239).
        "a6d940da69d2e43664a37d44f0a9e28ad7f5ca2ecb8ebb03f46cf05752acd94b",
    }),
    "vibe-entry": frozenset({
        "315588896b6b69c069858aeaa2107146e6cbc3914f31b129e8057198842a0c38",
        "ddf7b12525e99fc740b11557833cf7ef78c8b8c197dc96586a3c7bebbb555056",
        # 2026-10-17: complex mode needs >=3 parallel nodes and cross-interrupt work; replies continue the tier.
        "182f957ee264e44d4b3c9b70d053a74d9bb613d3610f4adc1d95be6415ebb7e5",
        "3ec0a771d482d92eba7f155f212dab97beb3a4a3b6712e16d7dc3d81394797eb",
        "a0d8ad6b24ed85982695214359b754a136315865ce76a4ccb36e03b9c4c9ef00",
        "08140f273188868532d5724d4fb199d1ee48ac46c80cbc2786f46fce91e4ddff",
        "15253735f70ec99071c59d56e590ec50a903841802737fd5cf61d925dc435321",
        "2033500aac595e39e51d2bf4c42aaf757cb8ab94522b0c289587f68b15c500c2",
        "209d83d6f70e0444dce589630b95662fd369d38b7e9e3dfd7677293c6554ce24",
        "42aa6fb24ca989b67973a92189fabf0b1e203206636901fd73f361c9773cd445",
        "6ab2be64c1366b290c2c779a61702d881d4ad53f59b688ccae423acf7a596df3",
        "8091f9d39f66ecb373956d41cf573073420b78be173c53371e51f94e2ee356a6",
        "8a231e030151ff3a2378aaf1d1cd95745c7374e7cb95b1d4935c641d53b7287d",
        "e0a321c246cd2636a31612d643d7982c91ab5b473c80056b67cd800c853cd78e",
        # 2026-10-14: heartbeat --accept-decision + N1 intervals.
        "cfff1ae864a712bb1d110e82c95bfac801f313b9f19470c37843bd3c78dae5b1",
    }),
    # Worker protocol delivered by reference (#216): a ref-mode dispatch pins
    # the file's sha256 and only accepts a digest published here.
    "visible-sdd-worker": frozenset({
        "30b226ff0a6711a91779785c28a993679e0a33c3aeb89e836cfd427194750945",
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


def protocol_path(name: str) -> Path:
    """Absolute path of the shipped protocol file; the name is a simple identifier."""
    load_protocol(name)  # validates the name and that the file exists
    return _HERE / (name + ".md")


def published_protocol_digest(name: str) -> str:
    """sha256 of the shipped file's bytes, only if the published table lists it.

    Fail-closed: an edited protocol file whose digest is not in
    ``SHIPPED_PROTOCOL_DIGESTS`` raises instead of being pinned.
    """
    digest = hashlib.sha256(protocol_path(name).read_bytes()).hexdigest()
    if digest not in SHIPPED_PROTOCOL_DIGESTS.get(name, frozenset()):
        raise ValueError(
            "protocol {} does not match any published digest".format(name)
        )
    return digest


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
