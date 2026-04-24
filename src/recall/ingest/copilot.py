"""Ingest VS Code Copilot Chat history.

Copilot stores chat sessions as JSON files under each workspace's storage dir:
    %APPDATA%/Code*/User/workspaceStorage/<hash>/chatSessions/*.json
    %APPDATA%/Code*/User/workspaceStorage/<hash>/chatEditingSessions/**/*.json

We walk all known roots (stable + Insiders) and parse anything that looks like
a chat session. Format has shifted across VS Code versions, so we stay defensive.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, Iterator

from ..db import DB
from ..hashing import sha1_text


def _vscode_roots() -> list[Path]:
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        for variant in ("Code", "Code - Insiders"):
            p = Path(appdata) / variant / "User" / "workspaceStorage"
            if p.exists():
                roots.append(p)
    home = Path.home()
    for variant in ("Code", "Code - Insiders"):
        p = home / ".config" / variant / "User" / "workspaceStorage"
        if p.exists():
            roots.append(p)
        p = home / "Library" / "Application Support" / variant / "User" / "workspaceStorage"
        if p.exists():
            roots.append(p)
    return roots


def _iter_chat_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        for sub in root.iterdir() if root.is_dir() else []:
            for d in ("chatSessions", "chatEditingSessions"):
                target = sub / d
                if target.exists():
                    yield from target.rglob("*.json")


def _extract_turns(doc: dict) -> list[dict]:
    """Best-effort extraction of (role, text, ts) from a Copilot chat JSON."""
    out: list[dict] = []
    requests = doc.get("requests") or doc.get("messages") or []
    for r in requests:
        if not isinstance(r, dict):
            continue
        # User turn
        msg = r.get("message") or {}
        user_text = ""
        if isinstance(msg, dict):
            user_text = msg.get("text") or ""
            if not user_text and isinstance(msg.get("parts"), list):
                user_text = " ".join(
                    p.get("text", "") for p in msg["parts"] if isinstance(p, dict)
                )
        ts = r.get("timestamp") or doc.get("creationDate") or int(time.time() * 1000)
        if isinstance(ts, str):
            try:
                ts = int(ts)
            except ValueError:
                ts = int(time.time() * 1000)
        ts_s = int(ts / 1000) if ts > 10**12 else int(ts)
        if user_text.strip():
            out.append({"role": "user", "text": user_text, "ts": ts_s})

        # Assistant response
        resp = r.get("response") or r.get("result") or []
        if isinstance(resp, list):
            chunks: list[str] = []
            for piece in resp:
                if isinstance(piece, dict):
                    chunks.append(piece.get("value", "") or piece.get("text", "") or "")
                elif isinstance(piece, str):
                    chunks.append(piece)
            text = "\n".join(c for c in chunks if c).strip()
            if text:
                out.append({"role": "assistant", "text": text, "ts": ts_s})
    return out


def ingest_copilot(db: DB, roots: list[Path] | None = None) -> dict:
    roots = roots or _vscode_roots()
    seen = inserted = 0
    for f in _iter_chat_files(roots):
        try:
            doc = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        turns = _extract_turns(doc)
        for t in turns:
            seen += 1
            body = t["text"]
            ch = sha1_text(f"copilot::{f.name}::{t['role']}::{body}")
            _id, ins = db.upsert_item(
                kind="chat",
                source="copilot",
                ref=str(f),
                title=f"[{t['role']}] {body[:80]}",
                body=body,
                meta={"role": t["role"], "file": str(f)},
                ts=t["ts"],
                content_hash=ch,
            )
            if ins:
                inserted += 1
    return {"seen": seen, "inserted": inserted, "roots": [str(r) for r in roots]}
