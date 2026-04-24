"""Tests for digest, watcher, and new ingesters (mocked external sources)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from recall.db import DB
from recall.digest import digest_sessions
from recall.ingest.agents import ingest_claude, ingest_cline
from recall.ingest.generic import ingest_jsonl


def _seed(db: DB, n: int = 8, source: str = "test") -> None:
    base = int(time.time()) - 10_000
    for i in range(n):
        from recall.hashing import sha1_text
        body = (
            f"Turn {i}: We decided to use Postgres because it scales. "
            "This sentence has reasonable length to be picked. "
            "Another statement about migrations and schema design here."
        )
        db.upsert_item(
            kind="chat",
            source=source,
            ref="session-A",
            title=f"[user] turn {i}",
            body=body,
            ts=base + i * 60,
            content_hash=sha1_text(f"{source}::{i}::{body}"),
        )


def test_digest_creates_summaries(tmp_path: Path) -> None:
    db = DB(tmp_path / "r.db")
    _seed(db, n=10)
    res = digest_sessions(db, sources=["test"])
    assert res["digests_created"] >= 1
    rows = db.recent(kind="note")
    assert any(r["source"] == "digest" for r in rows)
    assert all(len(r["body"]) > 30 for r in rows)


def test_claude_jsonl_parser(tmp_path: Path, monkeypatch) -> None:
    """Simulate ~/.claude/projects/<x>/<id>.jsonl."""
    fake_home = tmp_path / "home"
    proj = fake_home / ".claude" / "projects" / "myproj"
    proj.mkdir(parents=True)
    log = proj / "abc.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(ev)
            for ev in [
                {"role": "user", "message": {"content": "hello claude"}, "timestamp": 1700000000},
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "text", "text": "hi back"}]},
                    "timestamp": 1700000010,
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    db = DB(tmp_path / "r.db")
    res = ingest_claude(db)
    assert res["inserted"] == 2


def test_cline_parser(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    task_dir = (
        appdata / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks" / "t1"
    )
    task_dir.mkdir(parents=True)
    (task_dir / "api_conversation_history.json").write_text(
        json.dumps(
            [
                {"role": "user", "content": "do the thing", "ts": 1700000000000},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "ts": 1700000010000,
                },
            ]
        ),
        encoding="utf-8",
    )
    db = DB(tmp_path / "r.db")
    res = ingest_cline(db)
    assert res["inserted"] == 2


def test_watcher_refreshes_hash(tmp_path: Path) -> None:
    from recall.staleness import check_staleness, mark_read
    from recall.watcher import _Handler  # noqa: PLC2701

    db = DB(tmp_path / "r.db")
    f = tmp_path / "code.py"
    f.write_text("a = 1\n")
    mark_read(db, [str(f)], agent="t")
    assert check_staleness(db, [str(f)])["stale"] == 0

    # simulate a save event after edit
    f.write_text("a = 2\n")
    handler = _Handler(db, tmp_path)

    class _E:
        is_directory = False
        src_path = str(f)

    handler.on_modified(_E())
    # File state hash refreshed; staleness now flagged because read_log holds old hash
    out = check_staleness(db, [str(f)])
    assert out["stale"] == 1
    assert out["results"][0]["reason"] == "modified_since_read"


def test_jsonl_still_works(tmp_path: Path) -> None:
    db = DB(tmp_path / "r.db")
    f = tmp_path / "x.jsonl"
    f.write_text(json.dumps({"role": "user", "text": "hi", "ts": 1}) + "\n", encoding="utf-8")
    res = ingest_jsonl(db, f)
    assert res["inserted"] == 1
