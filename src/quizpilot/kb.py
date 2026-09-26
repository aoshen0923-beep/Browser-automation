"""Local knowledge base: SQLite + FTS5 over pre-tokenized chunks.

Everything the contest needs to look up quickly (regulations, standards,
captured site pages, notes) lives here so answering takes seconds, not a
live browsing session.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .textutil import chunk, fts_query, index_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  module TEXT NOT NULL DEFAULT '',
  added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  page INTEGER,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens);
CREATE TABLE IF NOT EXISTS flows (
  name TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  updated REAL NOT NULL
);
"""


@dataclass
class Hit:
    chunk_id: int
    source: str
    title: str
    module: str
    page: int | None
    text: str
    score: float

    def cite(self) -> str:
        where = f" p.{self.page}" if self.page else ""
        return f"{self.title or self.source}{where} <{self.source}>"


def _locked(method):
    """Serialize access: the web UI answers several questions at once."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class KB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # The CLI runs lookups on a worker thread (asyncio.to_thread) while the
        # browser drives the event loop; calls never overlap, so sharing the
        # connection across threads is safe.
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "KB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @_locked
    def add_document(
        self,
        source: str,
        pages: list[tuple[int | None, str]],
        *,
        title: str = "",
        kind: str = "",
        module: str = "",
        added_at: float | None = None,
    ) -> int:
        """Insert or replace a document. `pages` is [(page_number, text)]."""
        with self.db:
            self._delete_source(source)
            cur = self.db.execute(
                "INSERT INTO docs(source, title, kind, module, added_at) VALUES (?,?,?,?,?)",
                (source, title, kind, module, added_at if added_at is not None else time.time()),
            )
            doc_id = cur.lastrowid
            head = f"{title}\n" if title else ""
            for page, text in pages:
                for piece in chunk(text):
                    cid = self.db.execute(
                        "INSERT INTO chunks(doc_id, page, text) VALUES (?,?,?)",
                        (doc_id, page, piece),
                    ).lastrowid
                    # The title is indexed with every chunk so a question that
                    # names the document finds all of its pages.
                    self.db.execute(
                        "INSERT INTO chunks_fts(rowid, tokens) VALUES (?,?)",
                        (cid, index_text(head + piece)),
                    )
            return doc_id

    def _delete_source(self, source: str) -> None:
        row = self.db.execute("SELECT id FROM docs WHERE source=?", (source,)).fetchone()
        if not row:
            return
        self.db.execute(
            "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)",
            (row[0],),
        )
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (row[0],))
        self.db.execute("DELETE FROM docs WHERE id=?", (row[0],))

    @_locked
    def remove(self, source: str) -> None:
        with self.db:
            self._delete_source(source)

    @_locked
    def get_text(self, source: str) -> str | None:
        """The stored text of one short document (notes, remembered answers)."""
        row = self.db.execute("SELECT id FROM docs WHERE source=?", (source,)).fetchone()
        if not row:
            return None
        return "\n".join(r[0] for r in self.db.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY id", (row[0],)))

    @_locked
    def search(self, text: str, k: int = 8, module: str | None = None, kind: str | None = None) -> list[Hit]:
        query = fts_query(text)
        if not query:
            return []
        sql = """
            SELECT c.id, d.source, d.title, d.module, c.page, c.text, bm25(chunks_fts) AS s
            FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN docs d ON d.id = c.doc_id
            WHERE chunks_fts MATCH ?
        """
        args: list[object] = [query]
        if module:
            sql += " AND d.module = ?"
            args.append(module)
        if kind:
            sql += " AND d.kind = ?"
            args.append(kind)
        sql += " ORDER BY s LIMIT ?"
        args.append(k)
        rows = self.db.execute(sql, args).fetchall()
        # bm25() is lower-is-better; flip the sign so higher means more relevant.
        return [Hit(r[0], r[1], r[2], r[3], r[4], r[5], -r[6]) for r in rows]

    @_locked
    def stats(self) -> dict[str, int]:
        docs = self.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"docs": docs, "chunks": chunks}

    @_locked
    def overview(self) -> dict:
        kinds = dict(self.db.execute("SELECT kind, COUNT(*) FROM docs GROUP BY kind").fetchall())
        modules = dict(self.db.execute(
            "SELECT module, COUNT(*) FROM docs WHERE module NOT IN ('', 'live', 'recipe', 'flow', 'memory') GROUP BY module"
        ).fetchall())
        return {
            **self.stats(),
            "flows": self.db.execute("SELECT COUNT(*) FROM flows").fetchone()[0],
            "recipes": kinds.get("recipe", 0),
            "answers": kinds.get("qa", 0),
            "lessons": kinds.get("lesson", 0),
            "sitenotes": kinds.get("sitenote", 0),
            "pages": kinds.get("page", 0) + kinds.get("live", 0),
            "captures": kinds.get("capture", 0),
            "files": kinds.get("pdf", 0) + kinds.get("html", 0) + kinds.get("text", 0),
            "modules": modules,
        }

    @_locked
    def documents(self, module: str | None = None) -> list[tuple[str, str, str, str]]:
        sql = "SELECT source, title, kind, module FROM docs"
        args: tuple = ()
        if module:
            sql += " WHERE module=?"
            args = (module,)
        return self.db.execute(sql + " ORDER BY added_at", args).fetchall()

    @_locked
    def export(self, dest: Path | str) -> Path:
        """Write a consistent copy of the whole KB to one file for sharing."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        out = sqlite3.connect(str(dest))
        try:
            self.db.backup(out)
        finally:
            out.close()
        return dest

    @_locked
    def merge_from(self, path: Path | str) -> dict[str, int]:
        """Merge a teammate's exported KB; for the same source the newer copy wins."""
        other = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
        counts = {"added": 0, "updated": 0, "kept": 0}
        try:
            try:
                docs = other.execute("SELECT id, source, title, kind, module, added_at FROM docs").fetchall()
            except sqlite3.DatabaseError as e:
                raise ValueError(f"{path} is not a quizpilot knowledge base") from e
            try:
                flows = other.execute("SELECT name, json, updated FROM flows").fetchall()
            except sqlite3.DatabaseError:
                flows = []  # exported before flows existed
            for name, text, updated in flows:
                mine = self.db.execute("SELECT updated FROM flows WHERE name=?", (name,)).fetchone()
                if not mine or mine[0] < updated:
                    with self.db:
                        self._put_flow(name, text, updated)
            for doc_id, source, title, kind, module, added_at in docs:
                mine = self.db.execute("SELECT added_at FROM docs WHERE source=?", (source,)).fetchone()
                if mine and mine[0] >= added_at:
                    counts["kept"] += 1
                    continue
                pages = other.execute(
                    "SELECT page, text FROM chunks WHERE doc_id=? ORDER BY id", (doc_id,)
                ).fetchall()
                self.add_document(source, pages, title=title, kind=kind, module=module, added_at=added_at)
                counts["updated" if mine else "added"] += 1
        finally:
            other.close()
        return counts

    # --- recorded flows ----------------------------------------------------------

    def _put_flow(self, name: str, text: str, updated: float) -> None:
        self.db.execute(
            "INSERT INTO flows(name, json, updated) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET json=excluded.json, updated=excluded.updated",
            (name, text, updated),
        )

    @_locked
    def save_flow(self, flow) -> None:
        with self.db:
            self._put_flow(flow.name, flow.to_json(), flow.updated)
        # A searchable card so questions can find the flow by its description.
        self.add_document(f"flow:{flow.name}", [(None, flow.summary())], title=f"流程：{flow.name}",
                          kind="flow", module="flow", added_at=flow.updated)

    @_locked
    def flows(self) -> list:
        from .flows import Flow

        return [Flow.from_json(r[0]) for r in self.db.execute("SELECT json FROM flows ORDER BY name").fetchall()]

    @_locked
    def get_flow(self, name: str):
        from .flows import Flow

        row = self.db.execute("SELECT json FROM flows WHERE name=?", (name,)).fetchone()
        return Flow.from_json(row[0]) if row else None

    @_locked
    def delete_flow(self, name: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM flows WHERE name=?", (name,))
            self._delete_source(f"flow:{name}")

    def find_flows(self, text: str, k: int = 3) -> list:
        names = [h.source.split(":", 1)[1] for h in self.search(text, k=k, kind="flow")]
        return [f for f in (self.get_flow(n) for n in names) if f is not None]
