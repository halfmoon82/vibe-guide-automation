"""Read-only migration and replay helpers for V2--V4.3 run history.

Legacy artifacts are copied into a separate history namespace.  Nothing in this
module writes to the current ``.vibe/runs`` namespace or mutates its source.
"""
from __future__ import annotations
import hashlib, json, os, shutil, tempfile
from pathlib import Path
from typing import Any, Dict, Iterable
from .paths import ProjectPaths

LEGACY_VERSIONS = ("2", "3", "4.0", "4.1", "4.2", "4.3")

def _sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def historical_run_path(paths: ProjectPaths, run_id: str) -> Path:
    if not isinstance(run_id,str) or not run_id or '/' in run_id or '\\' in run_id or run_id in {'.','..'}:
        raise ValueError('invalid historical run id')
    return paths.history_dir / run_id

def _files(source: Path):
    for p in sorted(source.rglob('*')):
        if p.is_symlink(): raise ValueError('historical source may not contain symlinks')
        if p.is_file(): yield p, p.relative_to(source)

def migrate_history(source: os.PathLike, paths: ProjectPaths, run_id: str) -> Dict[str, Any]:
    source=Path(source).expanduser().resolve(strict=False)
    if not source.is_dir(): raise ValueError('history source must be a directory')
    target=historical_run_path(paths,run_id)
    if target.exists() and any(target.iterdir()):
        manifest=target/'history_manifest.json'
        if manifest.is_file(): return json.loads(manifest.read_text())
        raise ValueError('historical destination already contains data')
    entries=[{'path':str(rel), 'sha256':_sha(p)} for p,rel in _files(source)]
    version='unknown'
    candidates = [source/'state.json', source/'config.json', source/'.vibe'/'state.json', source/'.vibe'/'config.json']
    # A legacy .vibe/runs/<id> may carry version metadata in its parent .vibe.
    if source.name and source.parent.name == 'runs':
        candidates.extend([source.parent.parent/'state.json', source.parent.parent/'config.json'])
    for p in candidates:
        if p.is_file():
            try:
                value=json.loads(p.read_text()); version=str(value.get('version',value.get('workflow_version','unknown')))
            except (OSError,ValueError,TypeError): version='unknown'
            if version != 'unknown': break
    status='migrated' if version != 'unknown' else 'historical_incomplete'
    target.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.history-',dir=str(target.parent)))
    try:
        payload=staging/'payload'; payload.mkdir()
        for p,rel in _files(source):
            out=payload/rel; out.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,out)
        manifest={'schema_version':1,'status':status,'run_id':run_id,'source':str(source),'source_version':version,'files':entries,'read_only':True}
        evidence={'schema_version':1,'run_id':run_id,'source_manifest_digest':hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest(),'interpretation':status,'current_execution_eligible':False}
        (staging/'history_manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
        (staging/'migration_evidence.json').write_text(json.dumps(evidence,sort_keys=True,indent=2)+'\n')
        os.replace(staging,target)
    finally:
        if staging.exists(): shutil.rmtree(staging,ignore_errors=True)
    return manifest

def replay_history(paths: ProjectPaths, run_id: str) -> Dict[str, Any]:
    root=historical_run_path(paths,run_id); mp=root/'history_manifest.json'
    if not mp.is_file(): raise ValueError('historical manifest is missing')
    manifest=json.loads(mp.read_text()); payload=root/'payload'; errors=[]
    for entry in manifest.get('files',[]):
        p=payload/entry['path']
        if not p.is_file() or _sha(p)!=entry['sha256']: errors.append(entry['path'])
    event=payload/'events.jsonl'; complete=not errors
    if event.is_file():
        try:
            previous=None; lines=event.read_text().splitlines()
            if not lines: complete=False; errors.append('events.jsonl:empty')
            for index,line in enumerate(lines,1):
                rec=json.loads(line)
                # New schema is strict. Only absent previous_event_digest may use
                # the explicitly supported legacy previous_digest spelling.
                if 'previous_event_digest' in rec:
                    link=rec['previous_event_digest']
                elif 'previous_digest' in rec:
                    link=rec['previous_digest']
                    if link == '': link=None
                else:
                    link=object()
                if link != previous:
                    complete=False; errors.append('events.jsonl:%d:chain' % index)
                supplied=rec.get('event_digest')
                digest_payload=dict(rec); digest_payload.pop('event_digest',None)
                expected=hashlib.sha256(json.dumps(digest_payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if not isinstance(supplied,str) or supplied != expected:
                    complete=False; errors.append('events.jsonl:%d:digest' % index)
                previous=supplied
        except (ValueError,TypeError,KeyError):
            complete=False; errors.append('events.jsonl:invalid')
    else:
        complete=False; errors.append('events.jsonl:missing')
    return {'status':'replayed' if complete and manifest.get('status')=='migrated' else 'historical_incomplete','run_id':run_id,'read_only':True,'current_execution_eligible':False,'errors':sorted(set(errors))}

