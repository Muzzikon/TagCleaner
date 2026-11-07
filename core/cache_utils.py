from __future__ import annotations

import hashlib
import json
import os
import time
import tempfile
import uuid
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TTL_DAYS = 30.0


def file_signature(file_path) -> dict[str, Any]:
    """Cheap file identity for caches that must invalidate after local changes."""
    path = Path(file_path)
    stat = path.stat()
    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = str(path.absolute())

    signature = {
        "path": os.path.normcase(resolved),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    # Tag writes deliberately restore mtime and may keep the same file size
    # (ID3 padding). A durable revision prevents reuse of the preceding plan.
    revision = read_json_if_fresh(_revision_path(path, canonical_path=signature['path']), ttl_days=None)
    if isinstance(revision, dict) and revision.get('revision'):
        signature['tag_write_revision'] = revision['revision']
    return signature


def _revision_path(file_path, canonical_path=None):
    key = canonical_path or os.path.normcase(str(Path(file_path).resolve()))
    return ROOT_DIR / 'cache' / 'file_revisions' / (stable_digest(key) + '.json')


def mark_file_changed(file_path):
    """Invalidate own pre-write checkpoints before a same-size, same-mtime edit."""
    target = _revision_path(file_path)
    atomic_write_json(target, {'revision': uuid.uuid4().hex})
    # Commit this marker before Mutagen can touch the music file.
    # Windows FlushFileBuffers requires a handle opened for writing.
    with target.open('r+b') as handle:
        os.fsync(handle.fileno())


def stable_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically replace a JSON cache file, safe for concurrent writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)

    # A fixed ``.tmp`` name races when two network workers resolve the same
    # identity at once. Give every writer its own temp file, then use replace.
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
        temp.replace(path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def read_json_if_fresh(path: Path, *, ttl_days: float | None = DEFAULT_TTL_DAYS):
    if not path.exists():
        return None

    if ttl_days is not None:
        max_age = max(0.0, float(ttl_days)) * 86400.0
        age = time.time() - path.stat().st_mtime
        if age > max_age:
            return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def cache_ttl_days(env_name: str, default: float = DEFAULT_TTL_DAYS) -> float | None:
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return default

    if raw.casefold() in {"none", "off", "infinite", "forever"}:
        return None

    try:
        return max(0.0, float(raw))
    except ValueError:
        return default
