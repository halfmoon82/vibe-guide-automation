"""Named digest helpers; each semantic has its own function."""

import hashlib
import json
from pathlib import Path
from typing import Any, Union


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Union[str, Path]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_digest(value: Any) -> str:
    """sha256 of sorted-key, compact, non-ASCII-preserving JSON."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(encoded)
