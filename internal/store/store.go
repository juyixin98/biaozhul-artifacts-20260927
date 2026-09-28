// Package store is the SQLite-backed persistence plus the local, synthetic
// fleet actuator. It is the only module allowed to touch the database and
// the only place where the "cluster" (a table of fake instance IDs) changes.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	_ "modernc.org/sqlite"

	"replicactl/internal/config"
)

// Store implements controller.Port against a single SQLite database.
type Store struct {
	db *sql.DB
}

const pragmas = "_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(ON)&_pragma=synchronous(NORMAL)"

// New opens (creating if needed) the database at dsn and applies migrations.
func New(ctx context.Context, dsn string) (*Store, error) {
	if dsn == "" {
		dsn = "file:replicactl.db"
	}
	full := dsn
	if !contains(dsn, "?") {
		full = dsn + "?" + pragmas
	}
	db, err := sql.Open("sqlite", full)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

const schema = `
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

-- Active configuration, single row at id=1; revision increments each replace.
CREATE TABLE IF NOT EXISTS config (
  id                INTEGER PRIMARY KEY CHECK (id = 1),
  revision          INTEGER NOT NULL,
  payload           TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

-- Synthetic fleet. active=1 rows are the current replicas; removed rows stay
-- for audit, giving every instance lifecycle a stable, non-reused identity.
CREATE TABLE IF NOT EXISTS instances (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  id         TEXT NOT NULL UNIQUE,
  active     INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  removed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_instances_active ON instances(active);

-- Load reports keyed by synthetic instance.
CREATE TABLE IF NOT EXISTS samples (
  instance_id TEXT NOT NULL,
  metric      TEXT NOT NULL,
  value       REAL NOT NULL,
  observed_at TEXT NOT NULL,
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_inst_obs ON samples(instance_id, metric, observed_at);
CREATE INDEX IF NOT EXISTS idx_samples_obs ON samples(observed_at);

-- External scale-from-zero work signal (local fixture).
CREATE TABLE IF NOT EXISTS demand (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  pending     INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_demand_obs ON demand(observed_at);

-- Post-hysteresis recommendations feeding the downscale stable window.
CREATE TABLE IF NOT EXISTS downscale_observations (
  at       TEXT PRIMARY KEY,
  replicas INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_at ON downscale_observations(at);

-- Explainable decision audit log.
CREATE TABLE IF NOT EXISTS decisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  TEXT NOT NULL,
  tick_at     TEXT NOT NULL,
  action      TEXT NOT NULL,
  payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_tick ON decisions(tick_at);

-- Monotonic counters (e.g. allocation sequence fallback).
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
`

func (s *Store) migrate(ctx context.Context) error {
	if _, err := s.db.ExecContext(ctx, schema); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	return nil
}

// SaveConfig replaces the active configuration and bumps its revision, but
// only when the serialized content actually changes. Restarting with the
// same configuration therefore keeps the revision stable (a restart is not a
// configuration change); PUTting a different config bumps it.
func (s *Store) SaveConfig(ctx context.Context, cfg config.Config, now string) (int64, error) {
	payload, err := json.Marshal(cfg)
	if err != nil {
		return 0, err
	}
	var revision int64
	err = s.withTx(ctx, func(tx *sql.Tx) error {
		var existing sql.NullString
		_ = tx.QueryRowContext(ctx, `SELECT payload FROM config WHERE id=1`).Scan(&existing)
		if existing.Valid && existing.String == string(payload) {
			r := int64(0)
			if err := tx.QueryRowContext(ctx, `SELECT revision FROM config WHERE id=1`).Scan(&r); err != nil {
				return err
			}
			revision = r // unchanged config: revision stable across restarts
			return nil
		}
		if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(revision),0) FROM config WHERE id=1`).Scan(&revision); err != nil {
			return err
		}
		revision++
		_, err := tx.ExecContext(ctx,
			`INSERT INTO config(id,revision,payload,updated_at) VALUES(1,?,?,?)
			 ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, payload=excluded.payload, updated_at=excluded.updated_at`,
			revision, string(payload), now)
		return err
	})
	return revision, err
}

// LoadConfig returns the active configuration and its revision.
func (s *Store) LoadConfig(ctx context.Context) (config.Config, int64, error) {
	var payload string
	var revision int64
	err := s.db.QueryRowContext(ctx, `SELECT revision,payload FROM config WHERE id=1`).Scan(&revision, &payload)
	if err != nil {
		return config.Config{}, 0, err
	}
	var cfg config.Config
	if err := json.Unmarshal([]byte(payload), &cfg); err != nil {
		return config.Config{}, 0, fmt.Errorf("decode stored config: %w", err)
	}
	return cfg, revision, nil
}

func (s *Store) withTx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}
