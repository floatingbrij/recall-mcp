"""Stale-context detection.

Workflow:
  1. Agent calls `recall_mark_read([paths])` after reading files.
     We snapshot each file's current sha1 and store it.
  2. Later, agent calls `recall_check_staleness([paths])`.
     For each path we compare current sha1 vs the hash recorded at last read.
     If different (or never read, or file deleted), it's flagged.

This stops the "agent acts on a 3-day-old mental model" failure mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .db import DB
from .hashing import sha1_file


def mark_read(db: DB, paths: Iterable[str], agent: str | None = None) -> dict:
    marked: list[dict] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists() or not p.is_file():
            marked.append({"path": str(p), "status": "missing"})
            continue
        try:
            h = sha1_file(p)
        except OSError as e:
            marked.append({"path": str(p), "status": "error", "error": str(e)})
            continue
        st = p.stat()
        db.upsert_file_state(str(p), h, st.st_size, st.st_mtime)
        db.record_read(str(p), agent, h)
        marked.append({"path": str(p), "status": "ok", "hash": h})
    return {"marked": marked}


def check_staleness(db: DB, paths: Iterable[str]) -> dict:
    results: list[dict] = []
    for raw in paths:
        p = Path(raw)
        last = db.last_read(str(p))
        if not p.exists():
            results.append(
                {"path": str(p), "stale": True, "reason": "deleted_since_read" if last else "missing"}
            )
            continue
        current = sha1_file(p)
        if not last:
            results.append({"path": str(p), "stale": True, "reason": "never_read"})
            continue
        if current != last["hash_at_read"]:
            results.append(
                {
                    "path": str(p),
                    "stale": True,
                    "reason": "modified_since_read",
                    "read_at": last["ts"],
                    "old_hash": last["hash_at_read"],
                    "new_hash": current,
                }
            )
        else:
            results.append({"path": str(p), "stale": False, "read_at": last["ts"]})
    stale_count = sum(1 for r in results if r["stale"])
    return {"total": len(results), "stale": stale_count, "results": results}
