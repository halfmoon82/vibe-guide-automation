"""Provider-neutral V4.5 execution loop.

This module deliberately does not call the legacy Monitor or CLI authorization
entrypoints. It owns only bound-plan materialization and the Issue-local
 developer/reviewer/rework/acceptance state machine.
"""
from __future__ import annotations
import hashlib, json, os, tempfile, re
from pathlib import Path
from typing import Any, Mapping

_REMOTE = frozenset({'commit','push','create_pr','create_mr','merge'})
_FORBIDDEN = frozenset({'deploy','release','production_write','credentials','external_communication'})

def _sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def validate_remote_git_permissions(card: Mapping[str,Any]) -> bool:
    if not isinstance(card, Mapping): raise ValueError('authorization card must be an object')
    switch=card.get('remote_git_actions')
    if switch not in {'allow','deny'}: raise ValueError('remote_git_actions must be allow or deny')
    options=card.get('remote_git_actions_options', ['allow','deny'])
    if sorted(options) != ['allow','deny']: raise ValueError('remote_git_actions_options must contain allow and deny')
    perms=card.get('permissions', {})
    if not isinstance(perms, Mapping): raise ValueError('permissions must be an object')
    if switch == 'deny' and any(perms.get(a) is True or (isinstance(perms.get(a),str) and 'conditional_on_remote_git_actions_allow' in perms.get(a)) for a in _REMOTE):
        raise ValueError('remote Git permissions require remote_git_actions=allow')
    if switch == 'allow' and any(a in perms and perms.get(a) is False for a in _REMOTE):
        raise ValueError('remote_git_actions=allow contradicts disabled remote Git permission')
    if any(perms.get(a) is True for a in _FORBIDDEN):
        raise ValueError('deploy, release, production writes, credentials and external communication are forbidden')
    return True

def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.', dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(data,f,ensure_ascii=False,sort_keys=True,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def materialize_authorized_plan(project_root, card: Mapping[str,Any], sources: Mapping[str,Path]):
    """Materialize only SHA-bound rev6 evidence into ``.vibe/plans/<id>``."""
    validate_remote_git_permissions(card)
    if card.get('plan_revision') != 6: raise ValueError('unsupported plan revision')
    plan_id=card.get('plan_id')
    if not isinstance(plan_id,str) or not plan_id or '/' in plan_id or '\\' in plan_id or plan_id in {'.','..'}:
        raise ValueError('invalid plan id')
    root=Path(project_root).resolve(); declared=card.get('project_root')
    if declared and Path(declared).resolve()!=root: raise ValueError('project root mismatch')
    refs=card.get('evidence_refs')
    if not isinstance(refs,Mapping) or not refs: raise ValueError('authorization card has no evidence refs')
    loaded={}
    for name,digest in refs.items():
        if name not in sources: raise ValueError('missing bound evidence: '+str(name))
        path=Path(sources[name]).resolve()
        if not path.is_file() or _sha(path).lower()!=str(digest).lower(): raise ValueError('evidence SHA mismatch: '+str(name))
        text = path.read_text(encoding='utf8')
        try:
            loaded[name]=json.loads(text)
        except Exception:
            # Markdown evidence is accepted only when it contains a complete
            # fenced JSON contract; arbitrary prose is never guessed as a plan.
            loaded[name] = None
            for block in re.findall(r"```(?:json)?\s*\n(.*?)```", text, flags=re.I|re.S):
                try:
                    candidate=json.loads(block)
                except Exception:
                    continue
                if isinstance(candidate, Mapping) and ('nodes' in candidate or candidate.get('plan_id')==plan_id):
                    loaded[name]=candidate; break
            # Prose Markdown remains valid SHA-bound context; only the
            # structured workflow contract is required for materialization.
    evidence=next((v for v in loaded.values() if isinstance(v,Mapping) and v.get('plan_id')==plan_id and 'nodes' in v),None)
    if evidence is None:
        wf=next((v for v in loaded.values() if isinstance(v,Mapping) and v.get('revision')==6 and isinstance(v.get('dag'),Mapping)),None)
        if wf is not None:
            deps=wf['dag'].get('depends_on',{})
            ids=set(deps)
            for values in deps.values(): ids.update(values)
            evidence={'plan_id':plan_id,'nodes':[{'id':i,'depends_on':deps.get(i,[])} for i in sorted(ids)]}
    if evidence is None: raise ValueError('bound evidence does not contain plan and node contracts')
    nodes=evidence['nodes']
    if not isinstance(nodes,list) or not all(isinstance(n,Mapping) and isinstance(n.get('id'),str) for n in nodes): raise ValueError('bound node contracts are invalid')
    out=root/'.vibe'/'plans'/plan_id
    _atomic_json(out/'plan.json', {'plan_id':plan_id,'plan_revision':6,'status':'authorized','node_ids':[n['id'] for n in nodes], 'source_digests':dict(refs)})
    _atomic_json(out/'nodes.json', nodes)
    return out

class NativeLoop:
    def __init__(self,nodes):
        self.nodes={n['id']:{**n,'status':n.get('status','planned'),'writer':None,'reviewer':None,'findings':[]} for n in nodes}
    def ready(self):
        return sorted(i for i,n in self.nodes.items() if n['status']=='planned' and all(self.nodes.get(d,{}).get('status')=='accepted' for d in n.get('depends_on',[])))
    def bind_writer(self,issue,task_id):
        n=self.nodes[issue]
        if n['writer'] and n['writer']!=task_id: raise ValueError('Issue already has a writer')
        n['writer']=task_id; n['status']='developing'
    def writer(self,issue): return self.nodes[issue]['writer']
    def developer_done(self,issue):
        n=self.nodes[issue]
        if not n['writer']: raise ValueError('developer writer is required')
        n['status']='reviewing'
    def bind_reviewer(self,issue,task_id):
        n=self.nodes[issue]
        if task_id==n['writer']: raise ValueError('reviewer must be independent')
        if n['reviewer'] and n['reviewer']!=task_id: raise ValueError('reviewer identity is immutable')
        n['reviewer']=task_id
    def review(self,issue,findings=()):
        n=self.nodes[issue]
        if not n.get('writer') or not n.get('reviewer'):
            raise ValueError('developer and independent reviewer bindings are required')
        if n.get('status') not in {'reviewing','developing'}:
            raise ValueError('Issue is not ready for review')
        n['findings']=list(findings)
        n['status']='rework' if any((f.get('severity') if isinstance(f,Mapping) else f) in {'P0','P1','P2'} for f in n['findings']) else 'accepted'
    def rework(self,issue):
        n=self.nodes[issue]
        if not n['writer'] or not n['reviewer']: raise ValueError('reviewer and writer evidence required')
        n['status']='developing'

    def snapshot(self):
        return {k: dict(v) for k,v in self.nodes.items()}

    def run_issue(self, issue, writer_task, reviewer_task, findings=()):
        """Run one bound Issue through developer, independent review and acceptance."""
        self.bind_writer(issue, writer_task)
        self.developer_done(issue)
        self.bind_reviewer(issue, reviewer_task)
        self.review(issue, findings)
        if self.nodes[issue]['status'] != 'accepted':
            raise ValueError('Issue requires rework before acceptance')
        return self.nodes[issue]
    def accept(self,issue):
        n=self.nodes[issue]
        if n['status']!='accepted': raise ValueError('Issue is not review-accepted')

def resolve_authorization_input(value, project_root):
    """Resolve either a card JSON path or a plan id; a plan id is never opened as JSON."""
    root=Path(project_root).resolve()
    candidate=Path(value).expanduser() if isinstance(value,(str,Path)) else None
    if candidate is not None and candidate.suffix.lower()=='.json' and candidate.exists():
        if candidate.is_symlink() or not candidate.is_file(): raise ValueError('authorization card must be a regular file')
        try: card=json.loads(candidate.read_text(encoding='utf8'))
        except Exception as e: raise ValueError('authorization card JSON is invalid') from e
        if not isinstance(card,dict): raise ValueError('authorization card must be an object')
        return 'card',card
    plan_id=str(value).strip() if value is not None else ''
    if not plan_id or '/' in plan_id or '\\' in plan_id: raise ValueError('plan id must be a simple identifier')
    plan=root/'.vibe'/'plans'/plan_id/'plan.json'
    if not plan.is_file(): raise FileNotFoundError('blocked_unknown: materialized plan is missing for plan id '+plan_id)
    try: data=json.loads(plan.read_text(encoding='utf8'))
    except Exception as e: raise ValueError('materialized plan is invalid') from e
    if not isinstance(data,dict) or data.get('plan_id')!=plan_id: raise ValueError('materialized plan identity mismatch')
    return 'plan',data

def initialize_native_run(project_root, plan_id):
    """Create the minimal recoverable native-loop state; no provider or Monitor side effect."""
    root=Path(project_root).resolve(); vibe=root/'.vibe'; vibe.mkdir(parents=True,exist_ok=True)
    state=vibe/'state.json'
    if not state.exists(): _atomic_json(state, {'plan_id':plan_id,'status':'ready','loop':'native'})
    run=vibe/'runs'/plan_id; run.mkdir(parents=True,exist_ok=True)
    for name,payload in [('state.json',{'plan_id':plan_id,'status':'ready','loop':'native'}),('tasks.json',{'plan_id':plan_id,'tasks':{}}),('events.jsonl',None)]:
        path=run/name
        if not path.exists():
            if payload is None: path.touch()
            else: _atomic_json(path,payload)
    return run
