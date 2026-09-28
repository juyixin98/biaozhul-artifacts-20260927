// Package store persists desired-state resources and the controller's
// decision ledger in SQLite. The database belongs to the desired-state API;
// the controller talks to it only through HTTP, never directly.
package store

import (
	"database/sql"
	"fmt"

	_ "modernc.org/sqlite"
)

// Store is the desired-state persistence layer.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the SQLite database at dsn and applies the
// schema. A single pooled connection serializes writes; modernc.org/sqlite
// has no CGO requirement.
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// One connection avoids "database is locked" between concurrent writers;
	// the workload is a local controller, not a high-QPS service.
	db.SetMaxOpenConns(1)
	pragmas := []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA busy_timeout=5000",
		"PRAGMA foreign_keys=ON",
		"PRAGMA synchronous=NORMAL",
	}
	for _, p := range pragmas {
		if _, err := db.Exec(p); err != nil {
			db.Close()
			return nil, fmt.Errorf("pragma %q: %w", p, err)
		}
	}
	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate() error {
	_, err := s.db.Exec(schema)
	return err
}

const schema = `
CREATE TABLE IF NOT EXISTS resources (
    uid               TEXT PRIMARY KEY,
    namespace         TEXT NOT NULL,
    name              TEXT NOT NULL,
    generation        INTEGER NOT NULL,
    resource_version  INTEGER NOT NULL,
    spec              TEXT NOT NULL DEFAULT '{}',
    spec_hash         TEXT NOT NULL DEFAULT '',
    finalizers        TEXT NOT NULL DEFAULT '[]',
    deletion_ts       TEXT,
    status            TEXT NOT NULL DEFAULT '{}',
    annotations       TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(namespace, name)
);
`
