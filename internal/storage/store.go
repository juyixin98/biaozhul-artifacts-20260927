// Package storage persists analysis evidence in SQLite: ingested requests,
// per-decision diagnostic events, byte-level conflicts, archived packets and
// materialized generation views. Replay reads are served from these tables,
// so results survive a process restart and can be audited independently of
// the in-memory engine.
package storage

import (
	"database/sql"
	"fmt"

	_ "modernc.org/sqlite"
)

// Store is a SQLite-backed evidence repository.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the database at dsn and applies the schema.
// A typical dsn is "file:data/tcpreplay.db?_pragma=busy_timeout(5000)".
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("storage: open %s: %w", dsn, err)
	}
	// Single writer; modernc.org/sqlite serializes within one connection pool
	// entry, and our workload is request-scoped transactions.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(pragmaSQL); err != nil {
		db.Close()
		return nil, fmt.Errorf("storage: pragmas: %w", err)
	}
	if _, err := db.Exec(schemaSQL); err != nil {
		db.Close()
		return nil, fmt.Errorf("storage: schema: %w", err)
	}
	return &Store{db: db}, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

const pragmaSQL = `
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
`

const schemaSQL = `
CREATE TABLE IF NOT EXISTS requests(
  id            TEXT PRIMARY KEY,
  source        TEXT NOT NULL,           -- file name or 'json'
  policy        TEXT NOT NULL,
  preview       INTEGER NOT NULL,        -- payload previews explicitly enabled
  packet_count  INTEGER NOT NULL,
  created_at    TEXT NOT NULL            -- RFC3339 UTC
);

CREATE TABLE IF NOT EXISTS packets_archive(
  request_id  TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,          -- arrival order
  record_id   TEXT NOT NULL,
  flow        TEXT NOT NULL,
  direction   TEXT NOT NULL,
  raw_seq     INTEGER NOT NULL,
  payload_len INTEGER NOT NULL,
  flags       TEXT NOT NULL,
  ts          TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(request_id, idx)
);

CREATE TABLE IF NOT EXISTS events(
  request_id    TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  record_id     TEXT NOT NULL DEFAULT '',
  ts            TEXT NOT NULL DEFAULT '',
  code          TEXT NOT NULL,
  level         TEXT NOT NULL,
  flow          TEXT NOT NULL DEFAULT '',
  generation    INTEGER NOT NULL DEFAULT 0,
  direction     TEXT NOT NULL DEFAULT '',
  msg           TEXT NOT NULL,
  raw_seq       INTEGER NOT NULL DEFAULT 0,
  abs_start     INTEGER NOT NULL DEFAULT 0,
  abs_end       INTEGER NOT NULL DEFAULT 0,
  next_contig   INTEGER NOT NULL DEFAULT 0,
  fin_pos       INTEGER NOT NULL DEFAULT -1,
  payload_hex   TEXT NOT NULL DEFAULT '',
  preview_total INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(request_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_code ON events(request_id, code);
CREATE INDEX IF NOT EXISTS idx_events_level ON events(request_id, level);

CREATE TABLE IF NOT EXISTS conflicts(
  request_id   TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  conflict_id  TEXT NOT NULL,
  record_id    TEXT NOT NULL,
  flow         TEXT NOT NULL,
  generation   INTEGER NOT NULL,
  direction    TEXT NOT NULL,
  byte_offset  INTEGER NOT NULL,
  raw_seq      INTEGER NOT NULL,
  accepted     INTEGER NOT NULL,
  offered      INTEGER NOT NULL,
  accepted_by  TEXT NOT NULL,
  policy       TEXT NOT NULL,
  disposition  TEXT NOT NULL,
  ts           TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(request_id, conflict_id)
);
CREATE INDEX IF NOT EXISTS idx_conflicts_flow ON conflicts(request_id, flow, generation, direction);

CREATE TABLE IF NOT EXISTS generation_views(
  request_id  TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  flow        TEXT NOT NULL,
  generation  INTEGER NOT NULL,
  view_json   TEXT NOT NULL,
  PRIMARY KEY(request_id, flow, generation)
);
`
