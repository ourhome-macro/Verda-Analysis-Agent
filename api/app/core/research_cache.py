"""Bounded SQLite cache for public search metadata and fetched page bodies."""
from __future__ import annotations

import hashlib
import json
import time

from app.core import db
from app.core.research_contract import VERSION as RESEARCH_VERSION

VERSION = f"research-cache-v1:{RESEARCH_VERSION}"
MAX_ENTRIES_PER_KIND = 2000


def key_for(kind: str, *parts) -> str:
    packed = json.dumps([VERSION, kind, *parts], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _ready(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS research_cache ("
                 "kind TEXT NOT NULL, cache_key TEXT NOT NULL, value TEXT NOT NULL,"
                 "expires_at REAL NOT NULL, created_at REAL NOT NULL,"
                 "PRIMARY KEY(kind, cache_key))")
    conn.execute("CREATE INDEX IF NOT EXISTS research_cache_expiry ON research_cache(kind, expires_at)")


def get(kind: str, cache_key: str, *, max_age_seconds: int | None = None):
    with db._LOCK:
        conn = db._connect()
        _ready(conn)
        row = conn.execute("SELECT value,created_at FROM research_cache WHERE kind=? AND cache_key=? AND expires_at>?",
                           (kind, cache_key, time.time())).fetchone()
    if row is None:
        return None
    if max_age_seconds is not None and row["created_at"] < time.time() - max_age_seconds:
        return None
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return None


def put(kind: str, cache_key: str, value, ttl_seconds: int):
    if ttl_seconds <= 0:
        return
    now = time.time()
    payload = json.dumps(value, ensure_ascii=False)
    with db._LOCK:
        conn = db._connect()
        _ready(conn)
        conn.execute("INSERT OR REPLACE INTO research_cache VALUES(?,?,?,?,?)",
                     (kind, cache_key, payload, now + ttl_seconds, now))
        conn.execute("DELETE FROM research_cache WHERE kind=? AND expires_at<=?", (kind, now))
        excess = conn.execute("SELECT count(*) FROM research_cache WHERE kind=?", (kind,)).fetchone()[0] - MAX_ENTRIES_PER_KIND
        if excess > 0:
            conn.execute("DELETE FROM research_cache WHERE rowid IN ("
                         "SELECT rowid FROM research_cache WHERE kind=? ORDER BY created_at LIMIT ?)",
                         (kind, excess))
        conn.commit()
