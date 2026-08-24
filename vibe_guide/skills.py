from dataclasses import dataclass, asdict
from pathlib import Path
import subprocess, datetime, json
@dataclass
class SkillSpec:
    name: str; source: str; commit: str
@dataclass
class SkillInstallResult:
    status: str; installed: bool; source: str; commit: str
def install_skill(spec, vibe_home, fetch=False):
    vendor=vibe_home/'vendor'/spec.name; target=vibe_home/'skills'/spec.name
    try:
        if fetch:
            if not vendor.exists(): subprocess.run(['git','clone',spec.source,str(vendor)],check=True,capture_output=True,text=True)
            subprocess.run(['git','-C',str(vendor),'fetch','--all'],check=True,capture_output=True,text=True)
        actual=subprocess.check_output(['git','-C',str(vendor),'rev-parse',spec.commit],text=True).strip()
        if not actual.startswith(spec.commit): raise RuntimeError('commit mismatch')
        if target.exists(): return SkillInstallResult('pending',False,spec.source,actual)
        target.parent.mkdir(parents=True,exist_ok=True); target.symlink_to(vendor, target_is_directory=True)
        record = {'source': spec.source.rstrip('/'), 'sha': actual, 'timestamp': datetime.datetime.utcnow().isoformat() + 'Z', 'validation': 'verified'}
        (vibe_home/'skills'/ (spec.name + '.json')).write_text(json.dumps(record) + '\n', encoding='utf-8')
        return SkillInstallResult('installed',True,spec.source.rstrip('/'),actual)
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        return SkillInstallResult('pending',False,spec.source,spec.commit)
