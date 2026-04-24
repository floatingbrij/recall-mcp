"""SQLite storage layer with vector search via sqlite-vec.

Schema overview
---------------
items          : every ingested unit (chat turn, commit, PR body, decision, note)
embeddings     : vec0 virtual table, item_id -> 384-dim vector
file_state     : per-file content hash + last-agent-read timestamp (staleness)
read_log       : append-only log of agent file reads
decisions      : extracted ADR-style records (links back to source items)
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

import sqlite_vec

EMBED_DIM = 384  # all-MiniLM-L6-v2 / bge-small

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,            -- chat | commit | pr | decision | note
    source       TEXT NOT NULL,            -- copilot | git | jsonl | manual | extracted
    ref          TEXT,                     -- sha, chat-id, file path, etc.
    title        TEXT,
    body         TEXT NOT NULL,
    meta_json    TEXT,                     -- arbitrary JSON
    ts           INTEGER NOT NULL,         -- unix seconds
    content_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_items_kind_ts ON items(kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS file_state (
    path           TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    size           INTEGER NOT NULL,
    mtime          REAL NOT NULL,
    last_indexed   INTEGER NOT NULL,
    last_read_by_agent INTEGER             -- unix seconds
);

CREATE TABLE IF NOT EXISTS read_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT NOT NULL,
    agent     TEXT,
    ts        INTEGER NOT NULL,
    hash_at_read TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_read_log_path ON read_log(path, ts DESC);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    rationale   TEXT,
    status      TEXT DEFAULT 'active',     -- active | superseded | rejected
    topic       TEXT,                      -- free-form tag
    source_item INTEGER REFERENCES items(id),
    superseded_by INTEGER REFERENCES decisions(id),
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_topic ON decisions(topic);

CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0(
    item_id INTEGER PRIMARY KEY,
    embedding FLOAT[{EMBED_DIM}]
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            self._conn.execute("BEGIN;")
            yield self._conn
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

    # ------------------------------------------------------------------ items
    def upsert_item(
        self,
        *,
        kind: str,
        source: str,
        body: str,
        content_hash: str,
        title: str | None = None,
        ref: str | None = None,
        meta: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> tuple[int, bool]:
        """Insert if new (by content_hash). Returns (item_id, inserted)."""
        ts = ts or int(time.time())
        cur = self._conn.execute(
            "SELECT id FROM items WHERE content_hash = ?", (content_hash,)
        )
        row = cur.fetchone()
        if row:
            return row["id"], False
        cur = self._conn.execute(
            """INSERT INTO items(kind, source, ref, title, body, meta_json, ts, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, source, ref, title, body, json.dumps(meta or {}), ts, content_hash),
        )
        return int(cur.lastrowid), True

    def add_embedding(self, item_id: int, vec: list[float]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO embeddings(item_id, embedding) VALUES (?, ?)",
            (item_id, sqlite_vec.serialize_float32(vec)),
        )

    def items_missing_embeddings(self, limit: int = 500) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """SELECT i.id, i.title, i.body FROM items i
                   LEFT JOIN embeddings e ON e.item_id = i.id
                   WHERE e.item_id IS NULL
                   LIMIT ?""",
                (limit,),
            )
        )

    def search(
        self,
        query_vec: list[float],
        kind: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT i.id, i.kind, i.source, i.ref, i.title, i.body, i.meta_json, i.ts,
                   v.distance
            FROM embeddings v
            JOIN items i ON i.id = v.item_id
            WHERE v.embedding MATCH ? AND k = ?
        """
        # sqlite-vec requires a `k = N` predicate for KNN
        params: list[Any] = [sqlite_vec.serialize_float32(query_vec), limit * 4]
        if kind:
            sql += " AND i.kind = ?"
            params.append(kind)
        sql += " ORDER BY v.distance LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def recent(self, kind: str | None = None, since: int | None = None, limit: int = 25):
        sql = "SELECT * FROM items WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params)]

    # ------------------------------------------------------------------ files
    def upsert_file_state(self, path: str, content_hash: str, size: int, mtime: float) -> None:
        self._conn.execute(
            """INSERT INTO file_state(path, content_hash, size, mtime, last_indexed)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 content_hash=excluded.content_hash,
                 size=excluded.size,
                 mtime=excluded.mtime,
                 last_indexed=excluded.last_indexed""",
            (path, content_hash, size, mtime, int(time.time())),
        )

    def get_file_state(self, path: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM file_state WHERE path = ?", (path,)
        ).fetchone()
        return dict(row) if row else None

    def record_read(self, path: str, agent: str | None, hash_at_read: str) -> None:
        ts = int(time.time())
        self._conn.execute(
            "INSERT INTO read_log(path, agent, ts, hash_at_read) VALUES (?, ?, ?, ?)",
            (path, agent, ts, hash_at_read),
        )
        self._conn.execute(
            "UPDATE file_state SET last_read_by_agent = ? WHERE path = ?", (ts, path)
        )

    def last_read(self, path: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM read_log WHERE path = ? ORDER BY ts DESC LIMIT 1", (path,)
        ).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------- decisions
    def add_decision(
        self,
        *,
        title: str,
        summary: str,
        rationale: str | None,
        topic: str | None,
        source_item: int | None,
        ts: int | None = None,
    ) -> int:
        ts = ts or int(time.time())
        cur = self._conn.execute(
            """INSERT INTO decisions(title, summary, rationale, topic, source_item, ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, summary, rationale, topic, source_item, ts),
        )
        return int(cur.lastrowid)

    def list_decisions(self, topic: str | None = None, limit: int = 50):
        sql = "SELECT * FROM decisions"
        params: list[Any] = []
        if topic:
            sql += " WHERE topic LIKE ?"
            params.append(f"%{topic}%")
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params)]

    # ----------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for (kind,) in self._conn.execute("SELECT DISTINCT kind FROM items"):
            out[kind] = self._conn.execute(
                "SELECT COUNT(*) FROM items WHERE kind = ?", (kind,)
            ).fetchone()[0]
        out["files_tracked"] = self._conn.execute(
            "SELECT COUNT(*) FROM file_state"
        ).fetchone()[0]
        out["decisions"] = self._conn.execute(
            "SELECT COUNT(*) FROM decisions"
        ).fetchone()[0]
        return out


def default_db_path() -> Path:
    """Per-workspace DB stored under .recall/recall.db."""
    return Path.cwd() / ".recall" / "recall.db"
