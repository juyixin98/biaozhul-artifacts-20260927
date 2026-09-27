"""Versioned SQLite storage and inverted index.

Tables (``index-1.0`` schema)::

    meta(key TEXT PRIMARY KEY, value TEXT)
        package_version, dsl_version, index_schema_version,
        corpus_version (sha256 of the loaded corpus file),
        schema_version (sha256 of the schema JSON),
        doc_count, built_at
    documents(doc_id TEXT PRIMARY KEY, doc_json TEXT NOT NULL,
              ord INTEGER NOT NULL)
    index_terms(field TEXT, term TEXT, doc_id TEXT,
                positions TEXT NOT NULL)         -- analyzed text tokens
    UNIQUE(field, term, doc_id)
    index_keyword(field TEXT, value TEXT, doc_id TEXT)
    index_scalar(field TEXT, value TEXT, value_num INTEGER, doc_id TEXT)
        -- int: value canonical + numeric key; date: ISO value
    saved_queries(query_hash TEXT PRIMARY KEY, canonical TEXT NOT NULL,
                  source TEXT, dsl_version TEXT, index_schema_version TEXT,
                  corpus_version TEXT, created_at TEXT NOT NULL)

The index is rebuilt from the corpus file whenever its sha256 differs
from the stored ``corpus_version`` (or the schema hash changes), so a
stale index can never silently serve a new corpus.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from searchdsl import DSL_SPEC_VERSION, INDEX_SCHEMA_VERSION, __version__
from searchdsl.analysis import parse_date, parse_int, tokenize
from searchdsl.spec import Schema, schema_from_dict

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    doc_json TEXT NOT NULL,
    ord INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS index_terms (
    field TEXT NOT NULL,
    term TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    positions TEXT NOT NULL,
    PRIMARY KEY (field, term, doc_id)
);
CREATE TABLE IF NOT EXISTS index_keyword (
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    PRIMARY KEY (field, value, doc_id)
);
CREATE TABLE IF NOT EXISTS index_scalar (
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    value_num INTEGER,
    doc_id TEXT NOT NULL,
    PRIMARY KEY (field, value, doc_id)
);
CREATE TABLE IF NOT EXISTS saved_queries (
    query_hash TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    source TEXT,
    dsl_version TEXT NOT NULL,
    index_schema_version TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_terms_lookup ON index_terms(term, field);
CREATE INDEX IF NOT EXISTS idx_keyword_lookup ON index_keyword(field, value);
CREATE INDEX IF NOT EXISTS idx_scalar_lookup ON index_scalar(field, value);
"""


@dataclass(frozen=True)
class IndexVersion:
    package_version: str
    dsl_version: str
    index_schema_version: str
    corpus_version: str
    schema_version: str
    doc_count: int
    built_at: str

    def as_dict(self) -> dict:
        return {
            "package_version": self.package_version,
            "dsl_version": self.dsl_version,
            "index_schema_version": self.index_schema_version,
            "corpus_version": self.corpus_version,
            "schema_version": self.schema_version,
            "doc_count": self.doc_count,
            "built_at": self.built_at,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the single-process ASGI test client
        # reuse the connection; access remains serialized by the event loop.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- versions --------------------------------------------------------

    def versions(self) -> Optional[IndexVersion]:
        rows = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM meta")}
        if "dsl_version" not in rows:
            return None
        return IndexVersion(
            package_version=rows.get("package_version", ""),
            dsl_version=rows["dsl_version"],
            index_schema_version=rows["index_schema_version"],
            corpus_version=rows["corpus_version"],
            schema_version=rows["schema_version"],
            doc_count=int(rows.get("doc_count", "0")),
            built_at=rows.get("built_at", ""),
        )

    def _set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- build / rebuild -------------------------------------------------

    def build(self, schema: Schema, docs: list[dict], *, corpus_hash: str, schema_hash: str):
        c = self.conn
        c.execute("DELETE FROM index_terms")
        c.execute("DELETE FROM index_keyword")
        c.execute("DELETE FROM index_scalar")
        c.execute("DELETE FROM documents")
        c.execute("DELETE FROM meta")

        for ord_, doc in enumerate(docs):
            doc_id = str(doc["doc_id"])
            c.execute(
                "INSERT INTO documents(doc_id, doc_json, ord) VALUES(?, ?, ?)",
                (doc_id, json.dumps(doc, ensure_ascii=False, sort_keys=True), ord_),
            )
            fields = doc.get("fields", {})
            for fname, spec in schema.fields.items():
                if fname not in fields or fields[fname] is None:
                    continue
                raw = fields[fname]
                values = raw if spec.multi_valued else [raw]
                for value in values:
                    self._index_value(fname, spec.type, value, doc_id)

        now = datetime.now(timezone.utc).isoformat()
        meta = {
            "package_version": __version__,
            "dsl_version": DSL_SPEC_VERSION,
            "index_schema_version": INDEX_SCHEMA_VERSION,
            "corpus_version": corpus_hash,
            "schema_version": schema_hash,
            "doc_count": str(len(docs)),
            "built_at": now,
        }
        for k, v in meta.items():
            self._set_meta(k, v)
        c.commit()

    def _index_value(self, fname: str, ftype: str, value, doc_id: str):
        if ftype == "text":
            terms = tokenize(str(value))
            positions: dict[str, list[int]] = {}
            for pos, term in enumerate(terms):
                positions.setdefault(term, []).append(pos)
            for term, plist in positions.items():
                self.conn.execute(
                    "INSERT INTO index_terms(field, term, doc_id, positions) "
                    "VALUES(?, ?, ?, ?)",
                    (fname, term, doc_id, json.dumps(plist)),
                )
        elif ftype == "keyword":
            self.conn.execute(
                "INSERT OR IGNORE INTO index_keyword(field, value, doc_id) VALUES(?, ?, ?)",
                (fname, str(value), doc_id),
            )
        elif ftype == "int":
            num = parse_int(str(value))
            self.conn.execute(
                "INSERT OR IGNORE INTO index_scalar(field, value, value_num, doc_id) "
                "VALUES(?, ?, ?, ?)",
                (fname, str(num), num, doc_id),
            )
        elif ftype == "date":
            iso = parse_date(str(value))
            self.conn.execute(
                "INSERT OR IGNORE INTO index_scalar(field, value, value_num, doc_id) "
                "VALUES(?, ?, NULL, ?)",
                (fname, iso, doc_id),
            )
        else:  # pragma: no cover - schema validation prevents this
            raise ValueError(f"cannot index field {fname!r} of type {ftype!r}")

    def build_from_files(
        self,
        schema_path: str | Path,
        corpus_path: str | Path,
        *,
        force: bool = False,
    ) -> IndexVersion:
        schema_bytes = Path(schema_path).read_bytes()
        schema = schema_from_dict(json.loads(schema_bytes.decode("utf-8")))
        corpus_bytes = Path(corpus_path).read_bytes()
        corpus_hash = sha256_bytes(corpus_bytes)
        schema_hash = sha256_bytes(schema_bytes)

        existing = None if force else self.versions()
        if (
            existing is not None
            and existing.corpus_version == corpus_hash
            and existing.schema_version == schema_hash
            and existing.index_schema_version == INDEX_SCHEMA_VERSION
        ):
            return existing

        docs = [
            json.loads(line)
            for line in corpus_bytes.decode("utf-8").splitlines()
            if line.strip()
        ]
        ids = [str(d["doc_id"]) for d in docs]
        if len(ids) != len(set(ids)):
            raise ValueError("corpus contains duplicate doc_id values")
        self.build(schema, docs, corpus_hash=corpus_hash, schema_hash=schema_hash)
        v = self.versions()
        assert v is not None
        return v

    # -- document access -------------------------------------------------

    def all_doc_ids(self) -> list[str]:
        return [r["doc_id"] for r in self.conn.execute("SELECT doc_id FROM documents ORDER BY ord")]

    def get_doc(self, doc_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT doc_json FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return json.loads(row["doc_json"]) if row else None

    def term_positions(self, field: str, term: str) -> dict[str, list[int]]:
        rows = self.conn.execute(
            "SELECT doc_id, positions FROM index_terms WHERE field = ? AND term = ?",
            (field, term),
        )
        return {r["doc_id"]: json.loads(r["positions"]) for r in rows}

    def term_docs(self, field: str, term: str) -> set[str]:
        return set(self.term_positions(field, term))

    def keyword_docs(self, field: str, value: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT doc_id FROM index_keyword WHERE field = ? AND value = ?",
            (field, value),
        )
        return {r["doc_id"] for r in rows}

    def scalar_docs(
        self,
        field: str,
        *,
        gte=None,
        gt=None,
        lte=None,
        lt=None,
        exact: Optional[str] = None,
        numeric: bool,
    ) -> set[str]:
        sql = "SELECT doc_id, value, value_num FROM index_scalar WHERE field = ?"
        params: list = [field]
        rows = self.conn.execute(sql, params).fetchall()
        out: set[str] = set()
        for r in rows:
            if numeric:
                v = r["value_num"]
                target = lambda raw: parse_int(raw)
            else:
                v = r["value"]
                target = lambda raw: parse_date(raw)
            if exact is not None:
                if v == target(exact):
                    out.add(r["doc_id"])
                continue
            ok = True
            if gte is not None and not v >= target(gte):
                ok = False
            if gt is not None and not v > target(gt):
                ok = False
            if lte is not None and not v <= target(lte):
                ok = False
            if lt is not None and not v < target(lt):
                ok = False
            if ok:
                out.add(r["doc_id"])
        return out

    # -- saved queries ---------------------------------------------------

    def save_query(
        self, query_hash: str, canonical: str, *, source: Optional[str], version: IndexVersion
    ) -> bool:
        """Insert a normalized query. Returns False if already stored."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO saved_queries(query_hash, canonical, source, "
            "dsl_version, index_schema_version, corpus_version, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                query_hash,
                canonical,
                source,
                version.dsl_version,
                version.index_schema_version,
                version.corpus_version,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_saved_query(self, query_hash: str) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT * FROM saved_queries WHERE query_hash = ?", (query_hash,)
        ).fetchone()
        return dict(r) if r else None

    def list_saved_queries(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT query_hash, source, created_at FROM saved_queries ORDER BY created_at"
            )
        ]


def open_store(path: str | Path, schema_path: str | Path, corpus_path: str | Path) -> Store:
    """Open the store and (re)build if the on-disk corpus/schema changed."""
    store = Store(path)
    store.build_from_files(schema_path, corpus_path)
    return store
