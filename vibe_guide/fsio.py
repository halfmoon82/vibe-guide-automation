"""Shared atomic write and strict JSON read helpers (new; callers migrate later)."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, Path]


class JsonMissingError(FileNotFoundError):
    """The JSON file does not exist."""


class JsonCorruptError(ValueError):
    """The JSON file exists but cannot be decoded."""


def atomic_write_bytes(path: PathLike, data: bytes) -> None:
    """Replace ``path`` atomically; the old content survives any mid-write failure."""
    target = Path(path)
    if target.is_symlink():
        raise ValueError("atomic write target may not be a symlink: %s" % target.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise ValueError("atomic write parent may not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + target.name + ".", dir=str(target.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(target))
        try:
            parent_descriptor = os.open(str(target.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def atomic_write_json(path: PathLike, payload: Any) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    atomic_write_bytes(path, encoded)


def read_json(path: PathLike) -> Any:
    """Read JSON, raising ``JsonMissingError`` or ``JsonCorruptError``; never ``{}``."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except FileNotFoundError as error:
        raise JsonMissingError("JSON file is missing: %s" % target.name) from error
    except OSError as error:
        raise JsonCorruptError("JSON file is unreadable: %s" % target.name) from error
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JsonCorruptError("JSON file is corrupt: %s" % target.name) from error
