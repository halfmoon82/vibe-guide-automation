from dataclasses import dataclass
from pathlib import Path
import shutil, subprocess
from typing import Optional
from .paths import ProjectPaths

@dataclass
class ScanReport:
    root: str; python_version: str; git_version: str; git_root: Optional[str]
    git_remote: Optional[str]; agentsmd_exists: bool; agentsmd_content: Optional[str]
    knowledge_exists: bool; vibe_exists: bool; skills: list

@dataclass
class PatchProposal:
    proposed: bool; content: str

def _run(*args):
    try: return subprocess.run(args, text=True, capture_output=True).stdout.strip()
    except OSError: return ""

def scan_project(paths):
    root=paths.root; git_root=_run('git','-C',str(root),'rev-parse','--show-toplevel') or None
    remote=_run('git','-C',str(root),'config','--get','remote.origin.url') or None
    py=_run('python3','--version'); gv=_run('git','--version')
    af=root/'AGENTS.md'; vibe=root/'.vibe'
    return ScanReport(str(root),py,gv,git_root,remote,af.exists(),af.read_text(encoding='utf-8') if af.exists() else None,(root/'.vibe/knowledge').is_dir(),vibe.is_dir(),[])

def build_agentsmd_patch(existing, report):
    if existing is not None: return PatchProposal(False, '')
    return PatchProposal(True, '# Vibe Guide\n\nProject guidance is maintained through the Vibe Guide.\n')
