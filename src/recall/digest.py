"""Session-digest summarizer.

Groups chat items into sessions (by source + ref + time gap), then produces a
compact summary per session. Stored as kind='note', source='digest' so digests
become first-class searchable items — letting agents fetch a 1-paragraph summary
instead of dozens of raw chat turns.

Two backends:
  * extractive (default, no LLM): top-N salient sentences via simple scoring
  * llm (optional): OpenAI-compatible chat completion
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Iterable

from .db import DB
from .hashing import sha1_text

SESSION_GAP_SEC = 60 * 60 * 2  # 2-hour idle gap = new session


def _group_sessions(rows: list[dict]) -> dict[str, list[dict]]:
    """Group chat rows into sessions keyed by (source|ref|session-index)."""
    by_ref: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_ref[(r["source"], r.get("ref") or "")].append(r)

    sessions: dict[str, list[dict]] = {}
    for (src, ref), items in by_ref.items():
        items.sort(key=lambda x: x["ts"])
        idx = 0
        last_ts = None
        for it in items:
            if last_ts is not None and it["ts"] - last_ts > SESSION_GAP_SEC:
                idx += 1
            key = f"{src}|{ref}|{idx}"
            sessions.setdefault(key, []).append(it)
            last_ts = it["ts"]
    return sessions


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _extractive_summary(turns: list[dict], max_sentences: int = 6) -> str:
    """Cheap extractive summary: prefer assistant turns + decision/question signals."""
    sentences: list[tuple[float, str]] = []
    for t in turns:
        role = (t.get("meta_json") or "").lower()
        body = t["body"] or ""
        weight = 1.5 if "assistant" in role else 1.0
        for s in _SENT_SPLIT.split(body):
            s = s.strip()
            if not (20 <= len(s) <= 300):
                continue
            score = weight
            if re.search(r"\b(decided|chose|switched|because|so we|therefore|TL;DR)\b", s, re.I):
                score += 1.5
            if re.search(r"\?$", s):
                score += 0.3
            sentences.append((score, s))
    sentences.sort(key=lambda x: -x[0])
    picked = []
    seen = set()
    for _, s in sentences:
        key = s.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        picked.append(s)
        if len(picked) >= max_sentences:
            break
    return " ".join(picked)


def _llm_summary(turns: list[dict], model: str = "gpt-4o-mini") -> str | None:
    if "OPENAI_API_KEY" not in os.environ:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    client = OpenAI()
    transcript = "\n".join(
        f"{(t.get('title') or '')[:6]}: {t['body'][:600]}" for t in turns[:60]
    )[:12000]
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this developer chat session in 4-6 sentences. "
                        "Focus on: decisions made, problems solved, files/areas touched, "
                        "and any open questions. No fluff."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            temperature=0.1,
        )
        return (resp.choices[0].message.content or "").strip() or None
    except Exception:
        return None


def digest_sessions(
    db: DB,
    sources: Iterable[str] | None = None,
    use_llm: bool = False,
    max_sessions: int = 200,
) -> dict:
    where = "kind = 'chat'"
    params: list = []
    if sources:
        srcs = list(sources)
        where += f" AND source IN ({','.join('?' for _ in srcs)})"
        params += srcs
    rows = [
        dict(r)
        for r in db._conn.execute(
            f"SELECT id, source, ref, title, body, ts, meta_json FROM items WHERE {where}",
            params,
        )
    ]
    sessions = _group_sessions(rows)
    created = 0
    for key, turns in list(sessions.items())[:max_sessions]:
        if len(turns) < 3:
            continue
        summary = (use_llm and _llm_summary(turns)) or _extractive_summary(turns)
        if not summary:
            continue
        first_ts = turns[0]["ts"]
        last_ts = turns[-1]["ts"]
        title = f"digest: {key} ({len(turns)} turns)"
        ch = sha1_text(f"digest::{key}::{first_ts}::{last_ts}::{len(turns)}")
        _id, ins = db.upsert_item(
            kind="note",
            source="digest",
            ref=key,
            title=title[:120],
            body=summary,
            meta={
                "session_key": key,
                "turn_count": len(turns),
                "from_ts": first_ts,
                "to_ts": last_ts,
                "source_ids": [t["id"] for t in turns][:50],
                "method": "llm" if use_llm else "extractive",
            },
            ts=last_ts,
            content_hash=ch,
        )
        if ins:
            created += 1
    return {"sessions": len(sessions), "digests_created": created}
