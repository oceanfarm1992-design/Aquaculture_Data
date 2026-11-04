"""
fingerprint.py — content hashes that ignore fields which change on every run.

Two runs that fetch the same data produce the same fingerprint, even though each row's
retrieved_at (download time) and source_url (which page it was on) differ. That is what
lets the archive and the database skip data they already have.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

# Columns that describe the download, not the data
VOLATILE_COLUMNS = frozenset({"retrieved_at", "source_url", "row_hash", "first_seen_at"})


def _normalise(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if value.is_integer():
            return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "{[":  # JSON text (e.g. geometry) and objects hash the same
            try:
                return _normalise(json.loads(stripped))
            except ValueError:
                pass
        return stripped or None
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def row_fingerprint(row: dict) -> str:
    """Hash of one row's data columns."""
    data = {k: _normalise(v) for k, v in row.items() if k not in VOLATILE_COLUMNS}
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def table_fingerprint(rows: Iterable[dict]) -> tuple[str, int]:
    """(hash, row count) of a whole table. Row order does not matter."""
    hashes = sorted(row_fingerprint(r) for r in rows)
    digest = hashlib.sha256("\n".join(hashes).encode("ascii")).hexdigest()
    return digest, len(hashes)
