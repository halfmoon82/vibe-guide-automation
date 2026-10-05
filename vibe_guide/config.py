"""Project-level configuration loaded from ``.vibe/config.json``.

The reader is deliberately small: it validates the fields the supervisor
depends on, falls back to documented defaults when a field (or the whole
file) is absent, and refuses to guess when a value is present but invalid.
An explicit but illegal value is a configuration error the user must fix,
not something to silently paper over.

``behavioral_command_blacklist_extra`` has append-only semantics: projects
can only *add* entries to the monitor's built-in behavioral-invariant
compile-command blacklist; the built-in list cannot be reduced or removed
through configuration.
"""
from dataclasses import dataclass
import json
from pathlib import Path

#: Field name in ``.vibe/config.json`` capping simultaneously active
#: developer/reviewer session pairs.
FIELD_MAX_ACTIVE_WORKER_SESSIONS = 'max_active_worker_sessions'

#: Field name in ``.vibe/config.json`` listing extra command prefixes that
#: are appended to the built-in behavioral-invariant compile-command
#: blacklist (append-only; see module docstring).
FIELD_BEHAVIORAL_COMMAND_BLACKLIST_EXTRA = 'behavioral_command_blacklist_extra'

#: Default applied when the file or the field is missing.
DEFAULT_MAX_ACTIVE_WORKER_SESSIONS = 5

MIN_MAX_ACTIVE_WORKER_SESSIONS = 1
MAX_MAX_ACTIVE_WORKER_SESSIONS = 64

#: Field name listing mechanical companion files (repository-relative paths)
#: the supervisor may pull into a node's file scope without asking the user,
#: in addition to files under ``tests/``.  The product default is empty: vibe
#: is a general tool and must not presume any project's layout.
FIELD_AUTO_SCOPE_PATHS = 'auto_scope_paths'

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
    #: Extra behavioral-invariant compile-command blacklist entries.
    #: Append-only: these extend the built-in blacklist, never shrink it.
    #: Defaults to an empty tuple so two-argument construction keeps working.
    behavioral_command_blacklist_extra: tuple = ()
    auto_scope_paths: tuple = ()


def is_repo_relative_path(value):
    """True for a normalized POSIX path that stays inside the repository.

    Rejects absolute paths, backslashes, drive prefixes and any empty, ``.``
    or ``..`` segment, so the string compared against scopes is exactly the
    file that will be written.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if value.startswith('/') or '\\' in value or ':' in value:
        return False
    return all(part not in ('', '.', '..') for part in value.split('/'))


def _auto_scope_paths(data):
    if FIELD_AUTO_SCOPE_PATHS not in data:
        return ()
    value = data[FIELD_AUTO_SCOPE_PATHS]
    if not isinstance(value, list) or not all(
        is_repo_relative_path(item) for item in value
    ):
        raise ValueError(
            f'{FIELD_AUTO_SCOPE_PATHS} must be a list of repository-relative '
            'paths without "." or ".." segments'
        )
    return tuple(dict.fromkeys(value))


def _config_path(root):
    return Path(root) / '.vibe' / 'config.json'


def _load_blacklist_extra(data):
    """Validate the append-only behavioral blacklist extension field.

    Absent field -> empty tuple.  A present value must be a list of
    non-empty strings; anything else is a configuration error naming the
    field, never a silent fallback.
    """
    if FIELD_BEHAVIORAL_COMMAND_BLACKLIST_EXTRA not in data:
        return ()
    value = data[FIELD_BEHAVIORAL_COMMAND_BLACKLIST_EXTRA]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(
            f'{FIELD_BEHAVIORAL_COMMAND_BLACKLIST_EXTRA} must be a list of '
            'non-empty strings'
        )
    return tuple(item.strip() for item in value)


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
    blacklist_extra = _load_blacklist_extra(data)
    auto_scope_paths = _auto_scope_paths(data)
    if FIELD_MAX_ACTIVE_WORKER_SESSIONS not in data:
        return ProjectConfig(
            DEFAULT_MAX_ACTIVE_WORKER_SESSIONS, SOURCE_DEFAULT, blacklist_extra,
            auto_scope_paths=auto_scope_paths,
        )
    value = data[FIELD_MAX_ACTIVE_WORKER_SESSIONS]
    # bool is a subclass of int; True/False are never a valid worker cap.
    if isinstance(value, bool) or not isinstance(value, int) or not (
        MIN_MAX_ACTIVE_WORKER_SESSIONS <= value <= MAX_MAX_ACTIVE_WORKER_SESSIONS
    ):
        raise ValueError(
            f'{FIELD_MAX_ACTIVE_WORKER_SESSIONS} must be an integer between '
            f'{MIN_MAX_ACTIVE_WORKER_SESSIONS} and {MAX_MAX_ACTIVE_WORKER_SESSIONS}'
        )
    return ProjectConfig(
        value, SOURCE_CONFIG, blacklist_extra, auto_scope_paths=auto_scope_paths
    )
