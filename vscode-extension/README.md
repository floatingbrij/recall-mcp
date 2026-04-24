# recall — VS Code extension

This extension contributes the **recall** MCP server to VS Code (1.95+), giving Copilot Chat / Agent persistent memory:

- 🔍 **Conversation Replay Index** — semantic search over your past Copilot/Cursor/Claude/Cline chats
- 📋 **Decision Log** — auto-extracted ADR-style decisions from chats, commits, and PRs
- 🔄 **Stale Context Detector** — warns agents when they're about to act on out-of-date file reads

## Requirements

- VS Code 1.95+
- Python 3.10+ on PATH (or set `recall.pythonPath`)
- The Python package `recall-mcp` (the extension will offer to `pip install` it on first run)

## How it works

1. Install this extension.
2. On activation, it locates a Python interpreter and ensures `recall-mcp` is installed.
3. It registers `recall` as an MCP server. Open the **MCP: List Servers** view and you'll see it ready.
4. Use it from Copilot Chat / Agent — tools like `recall_search`, `recall_check_staleness`, `recall_decisions`, etc. become available.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `recall.pythonPath` | (auto) | Override Python executable. |
| `recall.dbPath` | `${workspaceFolder}/.recall/recall.db` | SQLite DB location. |

## Source

https://github.com/floatingbrij/recall-mcp
