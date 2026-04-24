"""MCP server exposing recall as tools.

Run with:
    recall-mcp                 (stdio)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import decisions as decisions_mod
from . import staleness as staleness_mod
from .db import DB, default_db_path
from .digest import digest_sessions
from .embeddings import embed_one, embed
from .ingest.agents import ingest_claude, ingest_cline, ingest_cursor
from .ingest.copilot import ingest_copilot
from .ingest.generic import ingest_jsonl
from .ingest.git import ingest_git
from .ingest.github import ingest_github_prs


def _db() -> DB:
    path = os.environ.get("RECALL_DB") or str(default_db_path())
    return DB(path)


def _ensure_embeddings(db: DB, batch: int = 64) -> int:
    rows = db.items_missing_embeddings(limit=batch * 8)
    if not rows:
        return 0
    texts = [(r["title"] or "") + "\n" + (r["body"] or "") for r in rows]
    vecs = embed(texts)
    with db.tx():
        for r, v in zip(rows, vecs):
            db.add_embedding(r["id"], v)
    return len(rows)


def _format_results(rows: list[dict]) -> str:
    out = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(r.get("meta_json") or "{}")
        except Exception:
            pass
        out.append(
            {
                "id": r["id"],
                "kind": r["kind"],
                "source": r["source"],
                "ts": r["ts"],
                "title": r.get("title"),
                "snippet": (r.get("body") or "")[:400],
                "ref": r.get("ref"),
                "meta": meta,
                "distance": r.get("distance"),
            }
        )
    return json.dumps(out, indent=2)


server: Server = Server("recall")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="recall_search",
            description=(
                "Semantic search across ingested chats, commits, PRs, and decisions. "
                "Use this BEFORE re-reading large files — it returns the most relevant "
                "prior context (usually <500 tokens) instead of forcing a full file read."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["chat", "commit", "pr", "decision", "note"],
                        "description": "Optional kind filter.",
                    },
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="recall_recent",
            description="List recent items by kind (chat/commit/pr/decision).",
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "since": {"type": "integer", "description": "unix seconds"},
                    "limit": {"type": "integer", "default": 25},
                },
            },
        ),
        Tool(
            name="recall_decisions",
            description=(
                "List extracted architectural/technical decisions, optionally filtered by topic "
                "(auth, db, frontend, infra, testing, build, or any substring)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="recall_check_staleness",
            description=(
                "Given file paths the agent is about to ACT ON, returns which are stale "
                "relative to when they were last read by an agent. ALWAYS call this before "
                "editing files you read more than a few minutes ago."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name="recall_mark_read",
            description=(
                "Record that the agent has just read these files. Pairs with "
                "recall_check_staleness. Call this immediately after reading a file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "agent": {"type": "string"},
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name="recall_ingest",
            description=(
                "Trigger ingestion. source options: copilot | git | jsonl | github | "
                "cursor | claude | cline | all."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": [
                            "copilot", "git", "jsonl", "github",
                            "cursor", "claude", "cline", "all",
                        ],
                    },
                    "repo_path": {"type": "string"},
                    "jsonl_path": {"type": "string"},
                    "gh_repo": {"type": "string"},
                    "extract_decisions": {"type": "boolean", "default": True},
                },
                "required": ["source"],
            },
        ),
        Tool(
            name="recall_digest",
            description=(
                "Summarize chat sessions into compact 'digest' notes that become "
                "searchable. Call after large ingests or periodically. Set use_llm=true "
                "for higher-quality summaries (requires OPENAI_API_KEY)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "use_llm": {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="recall_stats",
            description="Counts of items by kind, files tracked, decisions stored.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    db = _db()

    if name == "recall_search":
        # opportunistically fill any missing embeddings
        _ensure_embeddings(db)
        q = arguments["query"]
        vec = embed_one(q)
        rows = db.search(vec, kind=arguments.get("kind"), limit=arguments.get("limit", 8))
        return [TextContent(type="text", text=_format_results(rows))]

    if name == "recall_recent":
        rows = db.recent(
            kind=arguments.get("kind"),
            since=arguments.get("since"),
            limit=arguments.get("limit", 25),
        )
        return [TextContent(type="text", text=_format_results(rows))]

    if name == "recall_decisions":
        rows = db.list_decisions(topic=arguments.get("topic"), limit=arguments.get("limit", 50))
        return [TextContent(type="text", text=json.dumps(rows, indent=2))]

    if name == "recall_check_staleness":
        result = staleness_mod.check_staleness(db, arguments["paths"])
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "recall_mark_read":
        result = staleness_mod.mark_read(db, arguments["paths"], arguments.get("agent"))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "recall_ingest":
        src = arguments["source"]
        if src == "copilot":
            res = ingest_copilot(db)
        elif src == "git":
            res = ingest_git(db, arguments.get("repo_path", "."))
        elif src == "jsonl":
            res = ingest_jsonl(db, arguments["jsonl_path"])
        elif src == "github":
            res = ingest_github_prs(db, repo=arguments.get("gh_repo"))
        elif src == "cursor":
            res = ingest_cursor(db)
        elif src == "claude":
            res = ingest_claude(db)
        elif src == "cline":
            res = ingest_cline(db)
        elif src == "all":
            res = {}
            for nm, fn in [
                ("copilot", lambda: ingest_copilot(db)),
                ("cursor", lambda: ingest_cursor(db)),
                ("claude", lambda: ingest_claude(db)),
                ("cline", lambda: ingest_cline(db)),
                ("git", lambda: ingest_git(db, arguments.get("repo_path", "."))),
                ("github", lambda: ingest_github_prs(db, repo=arguments.get("gh_repo"))),
            ]:
                try:
                    res[nm] = fn()
                except Exception as e:
                    res[nm] = {"error": str(e)}
        else:
            res = {"error": f"unknown source {src}"}
        if arguments.get("extract_decisions", True):
            res["decisions"] = decisions_mod.extract_heuristic(db)
        embedded = _ensure_embeddings(db, batch=128)
        res["embedded"] = embedded
        return [TextContent(type="text", text=json.dumps(res, indent=2))]

    if name == "recall_digest":
        res = digest_sessions(
            db,
            sources=arguments.get("sources"),
            use_llm=bool(arguments.get("use_llm", False)),
        )
        return [TextContent(type="text", text=json.dumps(res, indent=2))]

    if name == "recall_stats":
        return [TextContent(type="text", text=json.dumps(db.stats(), indent=2))]

    return [TextContent(type="text", text=json.dumps({"error": f"unknown tool {name}"}))]


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
