// Package store persists table versions and batches in SQLite and can replay
// the event stream to reconstruct any earlier version.
//
// Each Apply() batch is committed in one SQL transaction: either every change
// of the batch is durable at the new version, or none is. The current head
// version lives in a single meta row.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	_ "modernc.org/sqlite"

	"github.com/opp221/ribd/internal/netmodel"
	"github.com/opp221/ribd/internal/rib"
)

// Store is the SQLite-backed persister.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the SQLite database at dsn and runs
// migrations. A ":memory:" DSN gives an isolated in-process database.
func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("store: open %q: %w", dsn, err)
	}
	// One writer at a time; concurrent readers still work. modernc.org/sqlite
	// shares a single connection when busy_timeout and WAL are set.
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, `PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;`); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: pragmas: %w", err)
	}
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS meta (
			key TEXT PRIMARY KEY,
			value TEXT NOT NULL
		);`,
		`CREATE TABLE IF NOT EXISTS events (
			version INTEGER PRIMARY KEY,
			base_version INTEGER NOT NULL,
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
			payload TEXT NOT NULL
		);`,
		`CREATE TABLE IF NOT EXISTS routes_snapshot (
			version INTEGER NOT NULL,
			route_id TEXT NOT NULL,
			payload TEXT NOT NULL,
			PRIMARY KEY (version, route_id)
		);`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("store: migrate: %w", err)
		}
	}
	row := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM meta WHERE key='head_version'`)
	var n int
	if err := row.Scan(&n); err != nil {
		return fmt.Errorf("store: meta probe: %w", err)
	}
	if n == 0 {
		if _, err := s.db.ExecContext(ctx, `INSERT INTO meta(key,value) VALUES('head_version','0')`); err != nil {
			return fmt.Errorf("store: init head: %w", err)
		}
	}
	return nil
}

// Close releases the database.
func (s *Store) Close() error { return s.db.Close() }

// HeadVersion returns the persisted current version.
func (s *Store) HeadVersion(ctx context.Context) (uint64, error) {
	var v uint64
	err := s.db.QueryRowContext(ctx, `SELECT CAST(value AS INTEGER) FROM meta WHERE key='head_version'`).Scan(&v)
	if err != nil {
		return 0, fmt.Errorf("store: head version: %w", err)
	}
	return v, nil
}

// Persist implements rib.Persister: one transaction per batch.
func (s *Store) Persist(baseVersion, newVersion uint64, changes []rib.Change, full []netmodel.Route) error {
	return s.PersistCtx(context.Background(), baseVersion, newVersion, changes, full)
}

// PersistCtx is the context-aware form of Persist.
func (s *Store) PersistCtx(ctx context.Context, baseVersion, newVersion uint64, changes []rib.Change, full []netmodel.Route) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: begin: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck

	var head uint64
	if err := tx.QueryRowContext(ctx, `SELECT CAST(value AS INTEGER) FROM meta WHERE key='head_version'`).Scan(&head); err != nil {
		return fmt.Errorf("store: read head: %w", err)
	}
	if head != baseVersion {
		return fmt.Errorf("store: optimistic version conflict: stored head %d, batch base %d", head, baseVersion)
	}

	eventPayload, err := json.Marshal(struct {
		Base    uint64       `json:"base_version"`
		Changes []rib.Change `json:"changes"`
	}{Base: baseVersion, Changes: changes})
	if err != nil {
		return fmt.Errorf("store: marshal event: %w", err)
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO events(version, base_version, payload) VALUES(?,?,?)`,
		newVersion, baseVersion, string(eventPayload)); err != nil {
		return fmt.Errorf("store: insert event v%d: %w", newVersion, err)
	}

	if _, err := tx.ExecContext(ctx, `DELETE FROM routes_snapshot WHERE version=?`, newVersion); err != nil {
		return fmt.Errorf("store: clear snapshot: %w", err)
	}
	stmt, err := tx.PrepareContext(ctx, `INSERT INTO routes_snapshot(version, route_id, payload) VALUES(?,?,?)`)
	if err != nil {
		return fmt.Errorf("store: prepare snapshot: %w", err)
	}
	defer stmt.Close()
	for i := range full {
		raw, err := json.Marshal(full[i])
		if err != nil {
			return fmt.Errorf("store: marshal route: %w", err)
		}
		if _, err := stmt.ExecContext(ctx, newVersion, full[i].ID, string(raw)); err != nil {
			return fmt.Errorf("store: insert route %s: %w", full[i].ID, err)
		}
	}

	if _, err := tx.ExecContext(ctx, `UPDATE meta SET value=? WHERE key='head_version'`, newVersion); err != nil {
		return fmt.Errorf("store: update head: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit v%d: %w", newVersion, err)
	}
	return nil
}

// Event is one persisted batch plus its envelope.
type Event struct {
	Version     uint64       `json:"version"`
	BaseVersion uint64       `json:"base_version"`
	CreatedAt   string       `json:"created_at"`
	Changes     []rib.Change `json:"changes"`
}

// Events returns batches ordered by version, optionally up to and including
// upTo (0 means the whole stream).
func (s *Store) Events(ctx context.Context, upTo uint64) ([]Event, error) {
	q := `SELECT version, base_version, created_at, payload FROM events`
	args := []any{}
	if upTo > 0 {
		q += ` WHERE version <= ?`
		args = append(args, upTo)
	}
	q += ` ORDER BY version ASC`
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("store: events: %w", err)
	}
	defer rows.Close()
	var out []Event
	for rows.Next() {
		var ev Event
		var payload string
		if err := rows.Scan(&ev.Version, &ev.BaseVersion, &ev.CreatedAt, &payload); err != nil {
			return nil, fmt.Errorf("store: scan event: %w", err)
		}
		var body struct {
			Changes []rib.Change `json:"changes"`
		}
		if err := json.Unmarshal([]byte(payload), &body); err != nil {
			return nil, fmt.Errorf("store: event v%d payload: %w", ev.Version, err)
		}
		ev.Changes = body.Changes
		out = append(out, ev)
	}
	return out, rows.Err()
}

// RoutesAt returns the route set persisted for a version (from the snapshot
// table).
func (s *Store) RoutesAt(ctx context.Context, version uint64) ([]netmodel.Route, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT payload FROM routes_snapshot WHERE version=? ORDER BY route_id`, version)
	if err != nil {
		return nil, fmt.Errorf("store: routes at v%d: %w", version, err)
	}
	defer rows.Close()
	var out []netmodel.Route
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var r netmodel.Route
		if err := json.Unmarshal([]byte(raw), &r); err != nil {
			return nil, fmt.Errorf("store: route payload v%d: %w", version, err)
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
