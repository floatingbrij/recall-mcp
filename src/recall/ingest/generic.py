"""Generic JSONL ingester — drop-in for Cursor/Claude Code/Cline exports.

Expected line format (lenient):
    {"role": "user|assistant", "text": "...", "ts": 1714000000, "session": "...", "tool": "cursor"}
Unknown fields are kept in meta.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..db import DB
from ..hashing import sha1_text


def ingest_jsonl(db: DB, path: str | Path, source: str = "jsonl") -> dict:
    p = Path(path)
    inserted = seen = 0
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen += 1
            text = rec.get("text") or rec.get("content") or ""
            if not text:
                continue
            role = rec.get("role", "user")
            ts = int(rec.get("ts") or time.time())
            tool = rec.get("tool") or source
            ch = sha1_text(f"{tool}::{rec.get('session','')}::{role}::{text}")
            _id, ins = db.upsert_item(
                kind="chat",
                source=tool,
                ref=str(p),
                title=f"[{role}] {text[:80]}",
                body=text,
                meta=rec,
                ts=ts,
                content_hash=ch,
            )
            if ins:
                inserted += 1
    return {"seen": seen, "inserted": inserted, "file": str(p)}
