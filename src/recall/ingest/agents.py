"""Native log parsers for Cursor, Claude Code (CLI), and Cline (VS Code extension).

Storage locations (best-known as of 2025-2026; we stay defensive):

  Cursor    : %APPDATA%/Cursor/User/workspaceStorage/<hash>/state.vscdb
              %APPDATA%/Cursor/User/globalStorage/state.vscdb
              keys we care about live in `cursorDiskKV` / `ItemTable` tables.
  Claude    : ~/.claude/projects/<encoded>/<uuid>.jsonl
              Each line is a JSON event with role + content.
  Cline     : %APPDATA%/Code*/User/globalStorage/saoudrizwan.claude-dev/tasks/<id>/
              api_conversation_history.json, ui_messages.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from ..db import DB
from ..hashing import sha1_text


# ---------------------------------------------------------------- Cursor

def _cursor_dbs() -> list[Path]:
    out: list[Path] = []
    appdata = os.environ.get("APPDATA")
    home = Path.home()
    candidates = []
    if appdata:
        candidates.append(Path(appdata) / "Cursor" / "User")
    candidates += [
        home / ".config" / "Cursor" / "User",
        home / "Library" / "Application Support" / "Cursor" / "User",
    ]
    for base in candidates:
        if not base.exists():
            continue
        gs = base / "globalStorage" / "state.vscdb"
        if gs.exists():
            out.append(gs)
        ws_root = base / "workspaceStorage"
        if ws_root.exists():
            for sub in ws_root.iterdir():
                p = sub / "state.vscdb"
                if p.exists():
                    out.append(p)
    return out


def _cursor_extract(db_path: Path) -> Iterator[dict]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        for table in ("cursorDiskKV", "ItemTable"):
            try:
                rows = conn.execute(f"SELECT key, value FROM {table}").fetchall()
            except sqlite3.Error:
                continue
            for key, value in rows:
                if not isinstance(value, (str, bytes)):
                    continue
                try:
                    raw = value if isinstance(value, str) else value.decode("utf-8", "ignore")
                    doc = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                yield from _walk_cursor_doc(doc, source_key=str(key))
    finally:
        conn.close()


def _walk_cursor_doc(doc, source_key: str) -> Iterator[dict]:
    """Cursor's chat blobs vary; we walk for any dict with role+text-shaped fields."""
    stack = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            role = node.get("role") or node.get("type")
            text = (
                node.get("text")
                or node.get("content")
                or node.get("message")
                or node.get("richText")
            )
            if isinstance(text, list):
                text = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in text
                )
            if role in ("user", "assistant", "human", "ai") and isinstance(text, str) and text.strip():
                ts = node.get("timestamp") or node.get("createdAt") or 0
                if isinstance(ts, (int, float)) and ts > 10**12:
                    ts = int(ts / 1000)
                yield {
                    "role": "assistant" if role in ("assistant", "ai") else "user",
                    "text": text,
                    "ts": int(ts) if ts else int(time.time()),
                    "key": source_key,
                }
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def ingest_cursor(db: DB) -> dict:
    inserted = seen = 0
    files = _cursor_dbs()
    for f in files:
        for turn in _cursor_extract(f):
            seen += 1
            ch = sha1_text(f"cursor::{f}::{turn['key']}::{turn['role']}::{turn['text']}")
            _, ins = db.upsert_item(
                kind="chat",
                source="cursor",
                ref=str(f),
                title=f"[{turn['role']}] {turn['text'][:80]}",
                body=turn["text"],
                meta={"role": turn["role"], "db": str(f), "key": turn["key"]},
                ts=turn["ts"],
                content_hash=ch,
            )
            if ins:
                inserted += 1
    return {"seen": seen, "inserted": inserted, "dbs": [str(f) for f in files]}


# ---------------------------------------------------------------- Claude Code

def _claude_dirs() -> list[Path]:
    cands = [Path.home() / ".claude" / "projects"]
    for c in cands:
        if c.exists():
            return [c]
    return []


def ingest_claude(db: DB) -> dict:
    inserted = seen = 0
    roots = _claude_dirs()
    files: list[Path] = []
    for root in roots:
        files.extend(root.rglob("*.jsonl"))
    for f in files:
        try:
            with f.open("r", encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = _extract_claude_text(ev)
                    if not text:
                        continue
                    seen += 1
                    role = ev.get("role") or ev.get("type") or "assistant"
                    if role not in ("user", "assistant"):
                        role = "assistant" if "assistant" in role else "user"
                    ts = ev.get("timestamp") or ev.get("ts") or int(time.time())
                    if isinstance(ts, str):
                        try:
                            from datetime import datetime
                            ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                        except Exception:
                            ts = int(time.time())
                    ch = sha1_text(f"claude::{f.name}::{role}::{text}")
                    _, ins = db.upsert_item(
                        kind="chat",
                        source="claude-code",
                        ref=str(f),
                        title=f"[{role}] {text[:80]}",
                        body=text,
                        meta={"role": role, "file": str(f)},
                        ts=int(ts) if isinstance(ts, (int, float)) else int(time.time()),
                        content_hash=ch,
                    )
                    if ins:
                        inserted += 1
        except OSError:
            continue
    return {"seen": seen, "inserted": inserted, "files": len(files)}


def _extract_claude_text(ev: dict) -> str:
    msg = ev.get("message") or ev
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text" and p.get("text"):
                    parts.append(p["text"])
                elif p.get("text"):
                    parts.append(p["text"])
        return "\n".join(parts).strip()
    if isinstance(msg, dict) and isinstance(msg.get("text"), str):
        return msg["text"]
    return ""


# ---------------------------------------------------------------- Cline

def _cline_task_dirs() -> list[Path]:
    out: list[Path] = []
    appdata = os.environ.get("APPDATA")
    home = Path.home()
    bases: list[Path] = []
    if appdata:
        for v in ("Code", "Code - Insiders"):
            bases.append(Path(appdata) / v / "User" / "globalStorage")
    for v in ("Code", "Code - Insiders"):
        bases.append(home / ".config" / v / "User" / "globalStorage")
        bases.append(home / "Library" / "Application Support" / v / "User" / "globalStorage")
    for b in bases:
        td = b / "saoudrizwan.claude-dev" / "tasks"
        if td.exists():
            out.extend([d for d in td.iterdir() if d.is_dir()])
    return out


def ingest_cline(db: DB) -> dict:
    inserted = seen = 0
    tasks = _cline_task_dirs()
    for task in tasks:
        for fname in ("api_conversation_history.json", "ui_messages.json"):
            fp = task / fname
            if not fp.exists():
                continue
            try:
                doc = json.loads(fp.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(doc, list):
                continue
            for msg in doc:
                if not isinstance(msg, dict):
                    continue
                text = _extract_claude_text(msg) or msg.get("text") or ""
                if not text:
                    continue
                seen += 1
                role = msg.get("role") or msg.get("type") or "assistant"
                if role not in ("user", "assistant"):
                    role = "assistant" if "ai" in str(role) or "assistant" in str(role) else "user"
                ts = msg.get("ts") or msg.get("timestamp") or int(time.time())
                if isinstance(ts, (int, float)) and ts > 10**12:
                    ts = int(ts / 1000)
                ch = sha1_text(f"cline::{task.name}::{fname}::{role}::{text}")
                _, ins = db.upsert_item(
                    kind="chat",
                    source="cline",
                    ref=str(fp),
                    title=f"[{role}] {text[:80]}",
                    body=text,
                    meta={"role": role, "task": task.name, "file": fname},
                    ts=int(ts),
                    content_hash=ch,
                )
                if ins:
                    inserted += 1
    return {"seen": seen, "inserted": inserted, "tasks": len(tasks)}
