"""recall CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich import print
from rich.table import Table

from . import decisions as decisions_mod
from . import staleness as staleness_mod
from .db import DB, default_db_path
from .digest import digest_sessions
from .embeddings import embed, embed_one
from .ingest.agents import ingest_claude, ingest_cline, ingest_cursor
from .ingest.copilot import ingest_copilot
from .ingest.generic import ingest_jsonl
from .ingest.git import ingest_git
from .ingest.github import ingest_github_prs

app = typer.Typer(help="recall: replay index + decision log + staleness detector.")


def _db() -> DB:
    return DB(os.environ.get("RECALL_DB") or str(default_db_path()))


def _embed_pending(db: DB) -> int:
    rows = db.items_missing_embeddings(limit=2000)
    if not rows:
        return 0
    texts = [(r["title"] or "") + "\n" + (r["body"] or "") for r in rows]
    vecs = embed(texts)
    with db.tx():
        for r, v in zip(rows, vecs):
            db.add_embedding(r["id"], v)
    return len(rows)


@app.command()
def init() -> None:
    """Initialize the recall DB in ./.recall/."""
    db = _db()
    print(f"[green]ok[/green] db at {db.path}")


@app.command()
def ingest(
    source: str = typer.Argument(
        ..., help="copilot | git | jsonl | github | cursor | claude | cline"
    ),
    repo: Path = typer.Option(Path("."), help="repo path for source=git"),
    file: Path = typer.Option(None, help="JSONL file for source=jsonl"),
    gh_repo: str = typer.Option(None, help="owner/name for source=github (else inferred)"),
    state: str = typer.Option("all", help="github PR state: open|closed|merged|all"),
    limit: int = typer.Option(200, help="github PR limit"),
    extract_decisions: bool = typer.Option(True, help="run decision extractor after ingest"),
    embed_now: bool = typer.Option(True, help="embed new items now"),
) -> None:
    db = _db()
    if source == "copilot":
        res = ingest_copilot(db)
    elif source == "git":
        res = ingest_git(db, repo)
    elif source == "jsonl":
        if not file:
            raise typer.BadParameter("--file required for source=jsonl")
        res = ingest_jsonl(db, file)
    elif source == "github":
        res = ingest_github_prs(db, repo=gh_repo, state=state, limit=limit)
    elif source == "cursor":
        res = ingest_cursor(db)
    elif source == "claude":
        res = ingest_claude(db)
    elif source == "cline":
        res = ingest_cline(db)
    else:
        raise typer.BadParameter(
            "source must be copilot|git|jsonl|github|cursor|claude|cline"
        )
    print(res)
    if extract_decisions:
        print("[cyan]extracting decisions[/cyan]")
        print(decisions_mod.extract_heuristic(db))
    if embed_now:
        n = _embed_pending(db)
        print(f"[green]embedded[/green] {n} items")


@app.command("ingest-all")
def ingest_all(repo: Path = typer.Option(Path("."), help="git repo path")) -> None:
    """Run every available ingester in one shot (best-effort)."""
    db = _db()
    summary: dict = {}
    for name, fn in [
        ("copilot", lambda: ingest_copilot(db)),
        ("cursor", lambda: ingest_cursor(db)),
        ("claude", lambda: ingest_claude(db)),
        ("cline", lambda: ingest_cline(db)),
        ("git", lambda: ingest_git(db, repo)),
        ("github", lambda: ingest_github_prs(db)),
    ]:
        try:
            summary[name] = fn()
        except Exception as e:
            summary[name] = {"error": str(e)}
    summary["decisions"] = decisions_mod.extract_heuristic(db)
    summary["embedded"] = _embed_pending(db)
    print(summary)


@app.command()
def search(query: str, kind: str = typer.Option(None), limit: int = 8) -> None:
    db = _db()
    _embed_pending(db)
    vec = embed_one(query)
    rows = db.search(vec, kind=kind, limit=limit)
    t = Table("dist", "kind", "src", "title")
    for r in rows:
        t.add_row(
            f"{r.get('distance', 0):.3f}",
            r["kind"],
            r["source"],
            (r.get("title") or "")[:80],
        )
    print(t)


@app.command()
def decisions(topic: str = typer.Option(None), limit: int = 50) -> None:
    db = _db()
    rows = db.list_decisions(topic=topic, limit=limit)
    t = Table("id", "topic", "title")
    for r in rows:
        t.add_row(str(r["id"]), r.get("topic") or "-", r["title"][:90])
    print(t)


@app.command("mark-read")
def mark_read_cmd(paths: list[Path], agent: str = typer.Option(None)) -> None:
    db = _db()
    print(staleness_mod.mark_read(db, [str(p) for p in paths], agent))


@app.command("check-stale")
def check_stale_cmd(paths: list[Path]) -> None:
    db = _db()
    print(json.dumps(staleness_mod.check_staleness(db, [str(p) for p in paths]), indent=2))


@app.command()
def digest(
    sources: str = typer.Option(None, help="comma-separated source filter"),
    use_llm: bool = typer.Option(False, help="use OpenAI for higher-quality summaries"),
) -> None:
    db = _db()
    srcs = [s.strip() for s in sources.split(",")] if sources else None
    print(digest_sessions(db, sources=srcs, use_llm=use_llm))


@app.command()
def watch(root: Path = typer.Option(Path("."), help="root to watch")) -> None:
    """Run the file watcher (auto-refresh hashes + periodic embedding sweep)."""
    from .watcher import Watcher

    db = _db()
    w = Watcher(db, root.resolve())
    print(f"[green]watching[/green] {root.resolve()} (Ctrl+C to stop)")
    w.run_forever()


@app.command()
def stats() -> None:
    db = _db()
    print(db.stats())


@app.command()
def serve() -> None:
    """Run the MCP server (stdio)."""
    from .server import main as srv

    srv()


if __name__ == "__main__":
    app()
