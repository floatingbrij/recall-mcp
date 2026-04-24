"""Decision extractor.

Heuristic-first: scans chats / commits / PR bodies for decision-shaped lines
(verbs like 'decided', 'chose', 'picked', 'going with', 'switched to', 'rejected').
Optional LLM upgrade (`recall.decisions.extract_with_llm`) if `openai` is installed
and OPENAI_API_KEY is set.

Each detected decision is written to the `decisions` table AND mirrored as a
'decision' item so it's searchable alongside chats/commits.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

from .db import DB
from .hashing import sha1_text

DECISION_PATTERNS = [
    r"\b(decided|we decided|i decided)\b.+",
    r"\b(chose|picked|selected|went with|going with)\b.+",
    r"\b(switch(?:ed|ing) (?:from|to)|migrat(?:ing|ed) to)\b.+",
    r"\b(rejected|ruled out|won't use|not using)\b.+",
    r"\b(adopted|standardiz(?:e|ing) on)\b.+",
    r"^ADR[- ]\d+[: ].+",
]
PATTERN = re.compile("|".join(f"(?:{p})" for p in DECISION_PATTERNS), re.IGNORECASE)

TOPIC_HINTS = {
    "auth": ["auth", "login", "oauth", "jwt", "session"],
    "db": ["postgres", "mysql", "sqlite", "mongo", "database", "schema"],
    "frontend": ["react", "vue", "svelte", "next", "vite", "tailwind"],
    "infra": ["docker", "kubernetes", "k8s", "terraform", "azure", "aws", "gcp"],
    "testing": ["pytest", "jest", "vitest", "playwright", "cypress"],
    "build": ["webpack", "esbuild", "vite", "rollup", "bazel"],
}


def _guess_topic(text: str) -> str | None:
    low = text.lower()
    for topic, kws in TOPIC_HINTS.items():
        if any(k in low for k in kws):
            return topic
    return None


@dataclass
class Decision:
    title: str
    summary: str
    rationale: str | None
    topic: str | None
    source_item: int | None


def _extract_from_text(text: str) -> list[Decision]:
    out: list[Decision] = []
    for line in text.splitlines():
        line = line.strip(" -*•\t")
        if len(line) < 20 or len(line) > 400:
            continue
        if PATTERN.search(line):
            title = line[:120]
            out.append(
                Decision(
                    title=title,
                    summary=line,
                    rationale=None,
                    topic=_guess_topic(line),
                    source_item=None,
                )
            )
    return out


def extract_heuristic(db: DB, kinds: Iterable[str] = ("chat", "commit", "pr")) -> dict:
    """Scan items of given kinds and persist any decisions found."""
    seen = added = 0
    placeholders = ",".join("?" for _ in kinds)
    rows = list(
        db._conn.execute(
            f"SELECT id, title, body FROM items WHERE kind IN ({placeholders})",
            tuple(kinds),
        )
    )
    for row in rows:
        seen += 1
        for d in _extract_from_text(row["body"] or ""):
            d.source_item = row["id"]
            # de-dup via content_hash on the mirrored item
            ch = sha1_text(f"decision::{d.summary}")
            item_id, inserted = db.upsert_item(
                kind="decision",
                source="extracted",
                ref=str(row["id"]),
                title=d.title,
                body=d.summary,
                meta={"topic": d.topic, "source_item": row["id"]},
                content_hash=ch,
            )
            if inserted:
                db.add_decision(
                    title=d.title,
                    summary=d.summary,
                    rationale=d.rationale,
                    topic=d.topic,
                    source_item=row["id"],
                )
                added += 1
    return {"items_scanned": seen, "decisions_added": added}


def extract_with_llm(db: DB, model: str = "gpt-4o-mini", limit: int = 200) -> dict:
    """Optional: better extraction via OpenAI-compatible API."""
    if "OPENAI_API_KEY" not in os.environ:
        return {"error": "OPENAI_API_KEY not set"}
    try:
        from openai import OpenAI
    except ImportError:
        return {"error": "openai package not installed; pip install 'recall-mcp[llm]'"}

    client = OpenAI()
    rows = list(
        db._conn.execute(
            "SELECT id, body FROM items WHERE kind IN ('chat','commit','pr') "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
    )
    added = 0
    for row in rows:
        prompt = (
            "Extract any architectural/technical decisions from this text. "
            "Return JSON list of {title, summary, rationale, topic} or [] if none.\n\n"
            f"TEXT:\n{row['body'][:4000]}"
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            import json as _json

            data = _json.loads(resp.choices[0].message.content or "{}")
            decisions = data.get("decisions") or data.get("items") or []
            for d in decisions:
                if not isinstance(d, dict) or not d.get("summary"):
                    continue
                ch = sha1_text(f"decision::llm::{d['summary']}")
                _id, ins = db.upsert_item(
                    kind="decision",
                    source="extracted-llm",
                    ref=str(row["id"]),
                    title=(d.get("title") or d["summary"])[:120],
                    body=d["summary"],
                    meta=d,
                    content_hash=ch,
                )
                if ins:
                    db.add_decision(
                        title=(d.get("title") or d["summary"])[:120],
                        summary=d["summary"],
                        rationale=d.get("rationale"),
                        topic=d.get("topic"),
                        source_item=row["id"],
                    )
                    added += 1
        except Exception:
            continue
    return {"items_scanned": len(rows), "decisions_added": added}
