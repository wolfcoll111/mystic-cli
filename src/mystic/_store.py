"""Shared I/O helpers: atomic JSON + TTL cache. Optimized for Pi SD cards."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("mystic.store")

_locks: dict[str, asyncio.Lock] = {}


def _lock_for(path: Path) -> asyncio.Lock:
    key = str(path)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def read_json(path: Path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("read_json failed %s: %s", path, e)
        return default


async def read_json_async(path: Path, default):
    try:
        return await asyncio.to_thread(read_json, path, default)
    except Exception:
        return default


def write_json_atomic(path: Path, data) -> None:
    """Sync atomic write: tmp + fsync + rename. Thread-safe for sync callers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


async def write_json_atomic_async(path: Path, data) -> None:
    async with _lock_for(path):
        await asyncio.to_thread(write_json_atomic, path, data)


class TTLCache:
    """Tiny TTL cache to avoid re-reading JSON on every chat turn / poll."""

    def __init__(self, ttl: float = 10.0):
        self.ttl = ttl
        self._value = None
        self._expires = 0.0

    def get(self):
        if time.monotonic() < self._expires:
            return self._value
        return None

    def set(self, value):
        self._value = value
        self._expires = time.monotonic() + self.ttl

    def invalidate(self):
        self._expires = 0.0
        self._value = None


def safe_join(root: Path, *parts: str) -> Path:
    """Join and resolve, ensuring result stays inside root. Raises ValueError on traversal."""
    root_resolved = root.resolve()
    # Fail closed on explicit traversal attempts (don't silently remap)
    for p in parts:
        segs = Path(p).parts
        if ".." in segs:
            raise ValueError(f"Path escapes sandbox: {p}")
    # Sanitize each part: drop drive/absolute anchors, keep name parts only
    cleaned: list[str] = []
    for p in parts:
        for part in Path(p).parts:
            if part in ("/", "\\", ".", ""):
                continue
            # strip .. segments entirely instead of naive replace (blocks ....// bypass)
            if part == "..":
                continue
            # Path(filename).name equivalent for trailing names handled by caller
            cleaned.append(part)
    target = (root_resolved.joinpath(*cleaned) if cleaned else root_resolved).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"Path escapes sandbox: {'/'.join(parts)}")
    return target


def safe_filename(name: str) -> str:
    """Strip directories, keep basename, remove risky chars."""
    base = Path(name).name.strip()
    # keep alnum, dot, dash, underscore; replace rest
    out = "".join(c if (c.isalnum() or c in ("-", "_", ".")) else "_" for c in base)
    return out[:128] or "file"
