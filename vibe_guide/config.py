"""Project-level configuration loaded from ``.vibe/config.json``.

The reader is deliberately small: it validates the fields the supervisor
depends on, falls back to documented defaults when a field (or the whole
file) is absent, and refuses to guess when a value is present but invalid.
An explicit but illegal value is a configuration error the user must fix,
not something to silently paper over.
"""
from dataclasses import dataclass
import json
from pathlib import Path

#: Field name in ``.vibe/config.json`` capping simultaneously active
#: developer/reviewer session pairs.
FIELD_MAX_ACTIVE_WORKER_SESSIONS = 'max_active_worker_sessions'

#: Default applied when the file or the field is missing.
DEFAULT_MAX_ACTIVE_WORKER_SESSIONS = 5

MIN_MAX_ACTIVE_WORKER_SESSIONS = 1
MAX_MAX_ACTIVE_WORKER_SESSIONS = 64

#: `ProjectConfig.source` value when the default was applied.
SOURCE_DEFAULT = 'default'
#: `ProjectConfig.source` value when an explicit configured value was used.
SOURCE_CONFIG = 'config'


@dataclass(frozen=True)
class ProjectConfig:
    max_active_worker_sessions: int
    #: Where `max_active_worker_sessions` came from: SOURCE_DEFAULT or
    #: SOURCE_CONFIG.  Recorded so callers and logs can tell an intentional
    #: limit from an implicit fallback.
    source: str


def _config_path(root):
    return Path(root) / '.vibe' / 'config.json'


def load_project_config(root):
    """Load the project config for `root`.

    Missing file or missing field -> defaults with ``source == 'default'``.
    A present but invalid value raises ValueError naming the field; the
    error is never silently replaced by the default.
    """
    path = _config_path(root)
    if not path.exists():
        return ProjectConfig(DEFAULT_MAX_ACTIVE_WORKER_SESSIONS, SOURCE_DEFAULT)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ValueError(f'.vibe/config.json is invalid: {error}') from error
    if not isinstance(data, dict):
        raise ValueError('.vibe/config.json must contain a JSON object')
    if FIELD_MAX_ACTIVE_WORKER_SESSIONS not in data:
        return ProjectConfig(DEFAULT_MAX_ACTIVE_WORKER_SESSIONS, SOURCE_DEFAULT)
    value = data[FIELD_MAX_ACTIVE_WORKER_SESSIONS]
    # bool is a subclass of int; True/False are never a valid worker cap.
    if isinstance(value, bool) or not isinstance(value, int) or not (
        MIN_MAX_ACTIVE_WORKER_SESSIONS <= value <= MAX_MAX_ACTIVE_WORKER_SESSIONS
    ):
        raise ValueError(
            f'{FIELD_MAX_ACTIVE_WORKER_SESSIONS} must be an integer between '
            f'{MIN_MAX_ACTIVE_WORKER_SESSIONS} and {MAX_MAX_ACTIVE_WORKER_SESSIONS}'
        )
    return ProjectConfig(value, SOURCE_CONFIG)
