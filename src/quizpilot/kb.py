"""Local knowledge base: SQLite + FTS5 over pre-tokenized chunks.

Everything the contest needs to look up quickly (regulations, standards,
captured site pages, notes) lives here so answering takes seconds, not a
live browsing session.
"""

from __future__ import annotations

import sqlite3
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


class KB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "KB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add_document(
        self,
        source: str,
        pages: list[tuple[int | None, str]],
        *,
        title: str = "",
        kind: str = "",
        module: str = "",
    ) -> int:
        """Insert or replace a document. `pages` is [(page_number, text)]."""
        with self.db:
            self._delete_source(source)
            cur = self.db.execute(
                "INSERT INTO docs(source, title, kind, module, added_at) VALUES (?,?,?,?,?)",
                (source, title, kind, module, time.time()),
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

    def remove(self, source: str) -> None:
        with self.db:
            self._delete_source(source)

    def search(self, text: str, k: int = 8, module: str | None = None) -> list[Hit]:
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
        sql += " ORDER BY s LIMIT ?"
        args.append(k)
        rows = self.db.execute(sql, args).fetchall()
        # bm25() is lower-is-better; flip the sign so higher means more relevant.
        return [Hit(r[0], r[1], r[2], r[3], r[4], r[5], -r[6]) for r in rows]

    def stats(self) -> dict[str, int]:
        docs = self.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"docs": docs, "chunks": chunks}

    def documents(self, module: str | None = None) -> list[tuple[str, str, str, str]]:
        sql = "SELECT source, title, kind, module FROM docs"
        args: tuple = ()
        if module:
            sql += " WHERE module=?"
            args = (module,)
        return self.db.execute(sql + " ORDER BY added_at", args).fetchall()
