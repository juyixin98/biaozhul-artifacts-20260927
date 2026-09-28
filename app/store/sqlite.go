// Package store owns the SQLite persistence: schema migrations, the durable
// decision log, the scale-down evidence history and a durable local fleet
// fixture. Everything lives in one local file; no network participant exists.
package store

import (
	"database/sql"
	"fmt"

	_ "modernc.org/sqlite"
)

// SchemaVersion is the current user_version of the SQLite database.
const SchemaVersion = 1

// Open opens (creating if needed) the SQLite database at dsn and migrates it.
// dsn is a modernc SQLite DSN, e.g. "file:data/controller.db".
func Open(dsn string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite %q: %w", dsn, err)
	}
	// Single connection: SQLite serialised writes and deterministic test order.
	db.SetMaxOpenConns(1)
	if err := migrate(db); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func migrate(db *sql.DB) error {
	var version int
	if err := db.QueryRow(`PRAGMA user_version`).Scan(&version); err != nil {
		return fmt.Errorf("read schema version: %w", err)
	}
	if version > SchemaVersion {
		return fmt.Errorf("database schema version %d is newer than binary %d", version, SchemaVersion)
	}
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS decisions(
			id              INTEGER PRIMARY KEY AUTOINCREMENT,
			request_id      TEXT NOT NULL,
			tick_at         INTEGER NOT NULL,
			action          TEXT NOT NULL,
			current_replicas INTEGER NOT NULL,
			desired_replicas INTEGER NOT NULL,
			reasons_json    TEXT NOT NULL,
			failure_class   TEXT NOT NULL DEFAULT '',
			failure_detail  TEXT NOT NULL DEFAULT '',
			observation_json TEXT NOT NULL DEFAULT '',
			created_at      INTEGER NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_decisions_tick ON decisions(tick_at)`,
		// One evidence point per tick.
		`CREATE TABLE IF NOT EXISTS raw_points(
			tick_at     INTEGER PRIMARY KEY,
			raw_desired INTEGER NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS fleet_state(
			key   TEXT PRIMARY KEY,
			value INTEGER NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS samples(
			instance_id TEXT PRIMARY KEY,
			load        REAL NOT NULL,
			reported_at INTEGER NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS demand(
			id          INTEGER PRIMARY KEY CHECK (id = 1),
			present     INTEGER NOT NULL,
			reported_at INTEGER NOT NULL,
			has_signal  INTEGER NOT NULL DEFAULT 0
		)`,
	}
	for _, s := range stmts {
		if _, err := db.Exec(s); err != nil {
			return fmt.Errorf("migrate: %w (stmt: %.40s)", err, s)
		}
	}
	if _, err := db.Exec(`INSERT OR IGNORE INTO fleet_state(key,value) VALUES('replicas',0)`); err != nil {
		return err
	}
	if _, err := db.Exec(fmt.Sprintf(`PRAGMA user_version = %d`, SchemaVersion)); err != nil {
		return fmt.Errorf("set schema version: %w", err)
	}
	return nil
}
