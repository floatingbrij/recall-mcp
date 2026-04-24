"""GitHub PR + review fetcher via the `gh` CLI (no extra auth needed)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

from ..db import DB
from ..hashing import sha1_text


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _run_gh(args: list[str]) -> Any:
    out = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def ingest_github_prs(
    db: DB,
    repo: str | None = None,
    state: str = "all",
    limit: int = 200,
    include_reviews: bool = True,
) -> dict:
    """
    repo: 'owner/name'. If None, gh infers from the current repo.
    state: open | closed | merged | all
    """
    if not _gh_available():
        return {"error": "gh CLI not found in PATH"}

    cmd = [
        "pr", "list",
        "--state", state,
        "--limit", str(limit),
        "--json", "number,title,body,author,createdAt,mergedAt,state,url,labels",
    ]
    if repo:
        cmd += ["--repo", repo]

    try:
        prs = _run_gh(cmd) or []
    except RuntimeError as e:
        return {"error": str(e)}

    inserted_pr = inserted_rev = 0
    for pr in prs:
        body = pr.get("body") or ""
        title = pr.get("title") or f"PR #{pr['number']}"
        ts = _parse_iso(pr.get("mergedAt") or pr.get("createdAt"))
        ref = pr.get("url") or f"#{pr['number']}"
        ch = sha1_text(f"gh-pr::{ref}::{title}::{body}")
        full = f"# {title}\n\n{body}\n\nlabels: {[l.get('name') for l in pr.get('labels') or []]}"
        _id, ins = db.upsert_item(
            kind="pr",
            source="github",
            ref=ref,
            title=title[:120],
            body=full,
            meta={
                "number": pr["number"],
                "author": (pr.get("author") or {}).get("login"),
                "state": pr.get("state"),
                "url": pr.get("url"),
            },
            ts=ts,
            content_hash=ch,
        )
        if ins:
            inserted_pr += 1

        if include_reviews:
            rcmd = [
                "pr", "view", str(pr["number"]),
                "--json", "reviews,comments",
            ]
            if repo:
                rcmd += ["--repo", repo]
            try:
                detail = _run_gh(rcmd) or {}
            except RuntimeError:
                detail = {}
            for rev in (detail.get("reviews") or []) + (detail.get("comments") or []):
                txt = rev.get("body") or ""
                if not txt.strip():
                    continue
                rts = _parse_iso(rev.get("submittedAt") or rev.get("createdAt") or pr.get("createdAt"))
                rch = sha1_text(f"gh-review::{ref}::{rev.get('id')}::{txt}")
                _, rins = db.upsert_item(
                    kind="pr",
                    source="github-review",
                    ref=ref,
                    title=f"review on PR #{pr['number']}",
                    body=txt,
                    meta={"pr": pr["number"], "author": (rev.get("author") or {}).get("login")},
                    ts=rts,
                    content_hash=rch,
                )
                if rins:
                    inserted_rev += 1

    return {
        "prs_seen": len(prs),
        "prs_inserted": inserted_pr,
        "reviews_inserted": inserted_rev,
    }


def _parse_iso(s: str | None) -> int:
    if not s:
        return int(time.time())
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return int(time.time())
