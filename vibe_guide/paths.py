from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @classmethod
    def from_cwd(cls, cwd: Path):
        return cls(Path(cwd).resolve())

