"""File watcher: keeps file_state hashes fresh and opportunistically re-embeds new items.

When a tracked file changes, we update its hash so `check_staleness` sees the new
content. We DO NOT auto-mark-read on save (that would defeat the purpose); we only
update the on-disk state so staleness comparisons stay accurate.

Also runs a periodic embedding sweep so newly-ingested items become searchable.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .db import DB
from .embeddings import embed
from .hashing import sha1_file

EMBED_INTERVAL_SEC = 30
IGNORED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".recall", "dist", "build"}


def _embed_pending(db: DB, batch: int = 256) -> int:
    rows = db.items_missing_embeddings(limit=batch)
    if not rows:
        return 0
    texts = [(r["title"] or "") + "\n" + (r["body"] or "") for r in rows]
    vecs = embed(texts)
    with db.tx():
        for r, v in zip(rows, vecs):
            db.add_embedding(r["id"], v)
    return len(rows)


class _Handler(FileSystemEventHandler):
    def __init__(self, db: DB, root: Path):
        self.db = db
        self.root = root.resolve()

    def _ignored(self, path: str) -> bool:
        parts = set(Path(path).parts)
        return bool(parts & IGNORED_DIRS)

    def _refresh(self, path: str) -> None:
        if self._ignored(path):
            return
        p = Path(path)
        if not p.is_file():
            return
        try:
            h = sha1_file(p)
            st = p.stat()
            self.db.upsert_file_state(str(p), h, st.st_size, st.st_mtime)
        except OSError:
            pass

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._refresh(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._refresh(event.src_path)


class Watcher:
    def __init__(self, db: DB, root: Path):
        self.db = db
        self.root = root
        self._observer: Observer | None = None
        self._stop = threading.Event()
        self._embed_thread: threading.Thread | None = None

    def start(self) -> None:
        self._observer = Observer()
        self._observer.schedule(_Handler(self.db, self.root), str(self.root), recursive=True)
        self._observer.start()
        self._embed_thread = threading.Thread(target=self._embed_loop, daemon=True)
        self._embed_thread.start()

    def _embed_loop(self) -> None:
        while not self._stop.is_set():
            try:
                _embed_pending(self.db)
            except Exception:
                pass
            self._stop.wait(EMBED_INTERVAL_SEC)

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)

    def run_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
