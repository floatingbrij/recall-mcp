"""Smoke test — runs end-to-end with a temp DB, no network beyond first model fetch."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from recall.db import DB
from recall.decisions import extract_heuristic
from recall.ingest.generic import ingest_jsonl
from recall.staleness import check_staleness, mark_read


def test_end_to_end(tmp_path: Path) -> None:
    db = DB(tmp_path / "r.db")

    # 1. Generic JSONL ingest
    jsonl = tmp_path / "chats.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"role": "user", "text": "Should we use Postgres or Mongo?", "ts": 1700000000},
                {
                    "role": "assistant",
                    "text": "We decided to go with Postgres because of relational needs.",
                    "ts": 1700000010,
                },
                {"role": "user", "text": "Cool, switching to pnpm too.", "ts": 1700000020},
            ]
        ),
        encoding="utf-8",
    )
    res = ingest_jsonl(db, jsonl, source="test")
    assert res["inserted"] == 3

    # 2. Decision extraction (heuristic)
    dres = extract_heuristic(db)
    assert dres["decisions_added"] >= 2  # "decided to go with…" and "switching to pnpm"

    # 3. Staleness round trip
    f = tmp_path / "code.py"
    f.write_text("x = 1\n")
    mark_read(db, [str(f)], agent="pytest")
    s = check_staleness(db, [str(f)])
    assert s["stale"] == 0

    f.write_text("x = 2\n")
    s2 = check_staleness(db, [str(f)])
    assert s2["stale"] == 1
    assert s2["results"][0]["reason"] == "modified_since_read"

    # 4. Stats
    st = db.stats()
    assert st["chat"] == 3
    assert st["decisions"] >= 2
