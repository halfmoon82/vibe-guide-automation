from dataclasses import dataclass
@dataclass
class DoctorReport:
    ok: bool; issues: list
def doctor(report):
    issues=[]
    if not report.knowledge_exists: issues.append('missing .vibe/knowledge')
    return DoctorReport(not issues, issues)

