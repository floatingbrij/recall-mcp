"""Ingest git commits + (best-effort) PR descriptions from commit trailers."""

from __future__ import annotations

from pathlib import Path

from git import Repo

from ..db import DB
from ..hashing import sha1_text


def ingest_git(db: DB, repo_path: str | Path = ".", max_commits: int = 2000) -> dict:
    repo = Repo(repo_path, search_parent_directories=True)
    inserted = seen = 0
    for c in repo.iter_commits(max_count=max_commits):
        seen += 1
        msg = c.message.strip()
        body = (
            f"{msg}\n\n"
            f"author: {c.author.name} <{c.author.email}>\n"
            f"files: {', '.join(c.stats.files.keys())[:500]}"
        )
        ch = sha1_text(f"commit::{c.hexsha}")
        _id, ins = db.upsert_item(
            kind="commit",
            source="git",
            ref=c.hexsha,
            title=msg.splitlines()[0][:120] if msg else c.hexsha[:12],
            body=body,
            meta={
                "sha": c.hexsha,
                "author": c.author.name,
                "files_changed": list(c.stats.files.keys())[:50],
            },
            ts=int(c.committed_date),
            content_hash=ch,
        )
        if ins:
            inserted += 1

        # If commit body looks like a PR/merge with a description, also store as 'pr'
        if any(line.lower().startswith(("pr:", "pull request", "merge pull request")) for line in msg.splitlines()):
            ch2 = sha1_text(f"pr::{c.hexsha}")
            db.upsert_item(
                kind="pr",
                source="git",
                ref=c.hexsha,
                title=msg.splitlines()[0][:120],
                body=msg,
                meta={"sha": c.hexsha},
                ts=int(c.committed_date),
                content_hash=ch2,
            )
    return {"seen": seen, "inserted": inserted, "repo": str(repo.working_dir)}
