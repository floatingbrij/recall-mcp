# recall

A local-first **MCP server** that gives coding agents (Copilot, Claude Code, Cursor, Cline, etc.) three things they currently lack:

1. **Conversation Replay Index** — every chat you've had with an AI agent becomes searchable. Stop re-explaining yourself.
2. **Decision Log** — auto-extracted ADR-style decisions from your chats, commits, and PR descriptions. "Why did we pick Postgres?" → answered.
3. **Stale Context Detector** — agents flag when they're about to act on a file they read minutes/hours/days ago that has since changed. Kills the "edit-on-stale-mental-model" bug.

All three share one SQLite + vector store, so nothing is duplicated and the whole thing fits in a few hundred MB on disk.

---

## Install

### As a VS Code extension (easiest)

1. Install the **recall** extension from the VS Code Marketplace.
2. On first run it will offer to `pip install recall-mcp` into your Python.
3. Done — Copilot Chat / Agent will see `recall_*` tools.

### As a standalone MCP server

```powershell
pip install recall-mcp
```

First run downloads the `bge-small-en-v1.5` ONNX embedding model (~30 MB) and caches it. No network calls after that.

## Quick start (CLI)

```powershell
# 1. Initialize the local DB (./.recall/recall.db)
recall init

# 2. Pull in your existing context (pick any/all)
recall ingest copilot                   # VS Code Copilot chat history
recall ingest cursor                    # Cursor's SQLite chat store
recall ingest claude                    # Claude Code CLI (~/.claude/projects)
recall ingest cline                     # Cline VS Code extension tasks
recall ingest git --repo .              # commits + PR-shaped messages
recall ingest github                    # real GitHub PRs + reviews via gh CLI
recall ingest jsonl --file chats.jsonl  # generic JSONL drop-in
recall ingest-all                       # run every available source

# 3. Search across everything
recall search "why did we switch from mongo"

# 4. List decisions
recall decisions --topic db

# 5. Compress long histories into searchable summaries
recall digest                           # extractive (free)
recall digest --use-llm                 # better quality (needs OPENAI_API_KEY)

# 6. Background watcher: keeps file hashes fresh, periodic embedding sweep
recall watch --root .

# 7. Stats
recall stats
```

## Use as an MCP server

Add to your client config (example — VS Code / Claude Desktop / Cursor mcp.json):

```json
{
  "mcpServers": {
    "recall": {
      "command": "recall-mcp",
      "env": { "RECALL_DB": "C:/path/to/your/repo/.recall/recall.db" }
    }
  }
}
```

### Tools exposed

| Tool | Purpose |
|---|---|
| `recall_search`           | Semantic search over chats / commits / PRs / decisions / digests |
| `recall_recent`           | Recent items by kind |
| `recall_decisions`        | List extracted decisions, filterable by topic |
| `recall_check_staleness`  | Given paths, report which are stale vs last agent read |
| `recall_mark_read`        | Record that the agent just read these files |
| `recall_ingest`           | Trigger ingestion (copilot/cursor/claude/cline/git/github/jsonl/all) |
| `recall_digest`           | Summarize chat sessions into compact searchable notes |
| `recall_stats`            | Quick health check |

### Recommended agent workflow

Tell your agent (via `copilot-instructions.md` / `AGENTS.md` / system prompt):

> Before reading large files, call `recall_search` with a description of what you need — it usually returns the answer in <500 tokens.
> After reading a file, call `recall_mark_read`.
> Before editing files you read more than ~5 minutes ago, call `recall_check_staleness` and re-read any flagged paths.

## How decisions are extracted

- **Heuristic (default):** regex over chats/commits/PRs for decision verbs (`decided`, `chose`, `switched to`, `rejected`, `ADR-…`).
- **LLM (optional):** `pip install 'recall-mcp[llm]'` + `OPENAI_API_KEY`. Then run `python -c "from recall.decisions import extract_with_llm; from recall.db import DB, default_db_path; print(extract_with_llm(DB(default_db_path())))"`.

Both write to the same `decisions` table and mirror as searchable `decision` items.

## Storage layout

```
.recall/
└── recall.db        # SQLite (WAL) with sqlite-vec for embeddings
```

Per-workspace by default. Override with `RECALL_DB=/abs/path.db`.

## Why this saves tokens

| Without recall | With recall |
|---|---|
| Agent greps repo, reads 12 files (~40K tokens) to recover prior context | Agent calls `recall_search` once → ~500 tokens |
| Agent re-edits a file based on a stale read, you revert and re-prompt | `recall_check_staleness` flags it → agent re-reads only the changed file |
| You re-explain "we use pnpm not npm" every session | `recall_decisions --topic build` returns it |

## Roadmap

- [x] File watcher that updates embeddings on save
- [x] Cursor / Claude Code / Cline native log parsers
- [x] GitHub PR body / review fetcher
- [x] LLM-summarized "session digests" for very long chat histories
- [ ] Slack / Linear / Jira ingesters
- [ ] Per-repo embedding shards for cross-repo search

## License

MIT
