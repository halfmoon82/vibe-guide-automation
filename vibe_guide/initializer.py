import json
from dataclasses import dataclass
from .scanner import build_agentsmd_patch, scan_project
@dataclass
class InitResult:
    changed: bool; paths: list
def init_project(paths, confirm):
    if not confirm: return InitResult(False, [])
    report=scan_project(paths); root=paths.root; created=[]
    for rel in ('.vibe/knowledge','.vibe/proposals/agentsmd'):
        p=root/rel
        if not p.exists(): p.mkdir(parents=True); created.append(rel)
    (root/'.vibe').mkdir(exist_ok=True)
    cfg=root/'.vibe/config.json'
    if not cfg.exists(): cfg.write_text('{}\n', encoding='utf-8'); created.append('.vibe/config.json')
    state=root/'.vibe/state.json'
    if not state.exists(): state.write_text('{}\n', encoding='utf-8'); created.append('.vibe/state.json')
    proposal=build_agentsmd_patch(report.agentsmd_content,report)
    pp=root/'.vibe/proposals/agentsmd/proposal.md'
    if proposal.proposed and not pp.exists(): pp.write_text(proposal.content,encoding='utf-8'); created.append(str(pp.relative_to(root)))
    return InitResult(bool(created), created)
