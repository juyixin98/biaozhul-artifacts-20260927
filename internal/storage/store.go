package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// Store is the SQLite-backed lease repository. One process owns one database
// file; access is serialized through database/sql with a busy timeout so
// concurrent goroutines block rather than fail with SQLITE_BUSY.
type Store struct {
	db     *sql.DB
	dsn    string
	poolIP map[string]struct{} // quick membership guard populated by state machine
}

// Open validates the DSN, applies pragmas, runs the schema migration and
// optionally wipes all prior state (fresh fixtures).
func Open(dsn string, reset bool) (*Store, error) {
	if !strings.Contains(dsn, "?") {
		dsn += "?"
	} else {
		dsn += "&"
	}
	// _pragma is understood by modernc.org/sqlite.
	dsn += "_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_pragma=journal_mode(WAL)&_pragma=synchronous(FULL)"

	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite %q: %w", redactDSN(dsn), err)
	}
	// A single connection makes BEGIN IMMEDIATE serialization explicit and
	// removes writer/reader lock surprises in the lab. State throughput here
	// is irrelevant; correctness under contention is the point.
	db.SetMaxOpenConns(1)
	db.SetConnMaxLifetime(0)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}

	s := &Store{db: db, dsn: dsn, poolIP: map[string]struct{}{}}
	if reset {
		if err := s.wipe(ctx); err != nil {
			_ = db.Close()
			return nil, err
		}
	}
	if err := s.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func redactDSN(dsn string) string {
	if i := strings.IndexByte(dsn, '?'); i >= 0 {
		return dsn[:i]
	}
	return dsn
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// ResetState wipes all leases, transactions and events but keeps the schema.
// Test-mode endpoint only.
func (s *Store) ResetState(ctx context.Context) error {
	for _, q := range []string{
		`DELETE FROM events`,
		`DELETE FROM transactions`,
		`DELETE FROM leases`,
		`DELETE FROM meta`,
	} {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("reset state: %w", err)
		}
	}
	return nil
}

func (s *Store) wipe(ctx context.Context) error {
	stmts := []string{
		`DROP TABLE IF EXISTS events`,
		`DROP TABLE IF EXISTS transactions`,
		`DROP TABLE IF EXISTS leases`,
		`DROP TABLE IF EXISTS meta`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("wipe: %w", err)
		}
	}
	return nil
}

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS meta (
			key TEXT PRIMARY KEY,
			value TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS leases (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			ip TEXT NOT NULL,
			identity_id TEXT NOT NULL,
			identity_label TEXT NOT NULL DEFAULT '',
			state TEXT NOT NULL CHECK (state IN ('OFFERED','BOUND','RELEASED','EXPIRED')),
			xid INTEGER NOT NULL DEFAULT 0,
			offered_at INTEGER NOT NULL DEFAULT 0,
			offer_exp INTEGER NOT NULL DEFAULT 0,
			bound_at INTEGER NOT NULL DEFAULT 0,
			starts INTEGER NOT NULL DEFAULT 0,
			ends INTEGER NOT NULL DEFAULT 0,
			renew_count INTEGER NOT NULL DEFAULT 0,
			updated_at INTEGER NOT NULL DEFAULT 0
		)`,
		// Uniqueness is enforced ONLY over active states via the partial
		// indexes below. A client may legitimately reappear at the same
		// address in a later lifecycle (after EXPIRED/RELEASED), so the full
		// history must not carry a plain UNIQUE(ip, identity_id) constraint.
		`CREATE INDEX IF NOT EXISTS idx_leases_ip_identity ON leases(ip, identity_id)`,
		// Partial unique index: at most one active record per address.
		`CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_active_ip
			ON leases(ip) WHERE state IN ('OFFERED','BOUND')`,
		// At most one active lease per client.
		`CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_active_identity
			ON leases(identity_id) WHERE state IN ('OFFERED','BOUND')`,
		`CREATE INDEX IF NOT EXISTS idx_leases_state_ends ON leases(state, ends)`,
		`CREATE TABLE IF NOT EXISTS transactions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			identity_id TEXT NOT NULL,
			xid INTEGER NOT NULL,
			phase TEXT NOT NULL,
			fingerprint TEXT NOT NULL,
			in_type TEXT NOT NULL,
			out_type TEXT NOT NULL,
			assigned_ip TEXT NOT NULL DEFAULT '',
			reply BLOB,
			lease_ends_at INTEGER NOT NULL DEFAULT 0,
			created_at INTEGER NOT NULL,
			UNIQUE(identity_id, xid, fingerprint)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_tx_lookup ON transactions(identity_id, xid)`,
		`CREATE TABLE IF NOT EXISTS events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			run_id TEXT NOT NULL,
			ts_nanos INTEGER NOT NULL,
			xid INTEGER NOT NULL,
			identity_id TEXT NOT NULL DEFAULT '',
			mac TEXT NOT NULL DEFAULT '',
			remote_addr TEXT NOT NULL DEFAULT '',
			in_type TEXT NOT NULL,
			out_type TEXT NOT NULL DEFAULT '',
			action TEXT NOT NULL,
			result TEXT NOT NULL,
			reason TEXT NOT NULL DEFAULT '',
			assigned_ip TEXT NOT NULL DEFAULT '',
			detail TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts_nanos)`,
		`CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id)`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("migrate: %w\nSQL: %s", err, q)
		}
	}
	return nil
}

// withImmediate runs fn inside BEGIN IMMEDIATE, retrying only on database
// lock contention (SQLITE_BUSY is already retried internally by the pragma;
// the extra loop covers lock-starvation bursts in concurrency tests).
func (s *Store) withImmediate(ctx context.Context, fn func(tx *sql.Tx) error) error {
	const attempts = 50
	var lastErr error
	for i := 0; i < attempts; i++ {
		tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
		if err != nil {
			if isBusy(err) {
				lastErr = err
				time.Sleep(time.Duration(2*(i+1)) * time.Millisecond)
				continue
			}
			return err
		}
		// With MaxOpenConns(1) plus journal_mode=WAL, this connection is the
		// sole writer; the partial unique indexes below enforce the address
		// uniqueness invariant atomically at commit.
		err = fn(tx)
		if err == nil {
			if err := tx.Commit(); err != nil {
				if isBusy(err) {
					lastErr = err
					continue
				}
				return err
			}
			return nil
		}
		_ = tx.Rollback()
		if isBusy(err) || errors.Is(err, ErrLeaseGone) {
			lastErr = err
			time.Sleep(time.Duration(2*(i+1)) * time.Millisecond)
			continue
		}
		return err
	}
	return fmt.Errorf("transaction gave up after %d attempts: %w", attempts, lastErr)
}

func isBusy(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "SQLITE_BUSY") ||
		strings.Contains(msg, "database table is locked") ||
		strings.Contains(msg, "database is locked")
}
