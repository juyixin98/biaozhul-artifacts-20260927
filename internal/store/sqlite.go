package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"fieldapply/internal/merge"
	"fieldapply/internal/model"
)

// SQLite is the durable Store adapter. Schema:
//
//	resources(id, revision, live, schema, created_at, updated_at)
//	owners(resource_id, path, manager)          -- one share per row
//	applied(resource_id, manager, config, at)   -- last-applied per manager
//	history(resource_id, revision, run_id, manager, reason, forced, changes, at)
type SQLite struct {
	db *sql.DB
}

// OpenSQLite opens (creating if needed) a SQLite database at dsn, e.g.
// "file:fieldapply.db?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)".
func OpenSQLite(dsn string) (*SQLite, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, wrapDB(err)
	}
	// SQLite connections are not safe for fully concurrent writers; modernc
	// serializes within one connection and busy_timeout handles contention.
	db.SetMaxOpenConns(1)
	s := &SQLite{db: db}
	if err := s.migrate(context.Background()); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLite) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS resources (
			id         TEXT PRIMARY KEY,
			revision   INTEGER NOT NULL,
			live       TEXT NOT NULL,
			schema_kv  TEXT NOT NULL DEFAULT '{}',
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS owners (
			resource_id TEXT NOT NULL REFERENCES resources(id),
			path        TEXT NOT NULL,
			manager     TEXT NOT NULL,
			PRIMARY KEY (resource_id, path, manager)
		)`,
		`CREATE TABLE IF NOT EXISTS applied (
			resource_id TEXT NOT NULL REFERENCES resources(id),
			manager     TEXT NOT NULL,
			config      TEXT NOT NULL,
			at          TEXT NOT NULL,
			PRIMARY KEY (resource_id, manager)
		)`,
		`CREATE TABLE IF NOT EXISTS history (
			resource_id TEXT NOT NULL,
			revision    INTEGER NOT NULL,
			run_id      TEXT NOT NULL,
			manager     TEXT NOT NULL,
			reason      TEXT NOT NULL,
			forced      INTEGER NOT NULL,
			changes     TEXT NOT NULL,
			at          TEXT NOT NULL,
			PRIMARY KEY (resource_id, revision)
		)`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return wrapDB(err)
		}
	}
	return nil
}

func (s *SQLite) Create(ctx context.Context, id string, live, applied json.RawMessage, manager string, schema model.Schema) (*Snapshot, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, wrapDB(err)
	}
	defer rollback(tx)

	var exists int
	if err := tx.QueryRowContext(ctx, `SELECT 1 FROM resources WHERE id = ?`, id).Scan(&exists); err == nil {
		return nil, &model.Error{Category: model.CatStateConflict, Code: "resource_exists",
			Message: "resource " + id + " already exists"}
	} else if !errors.Is(err, sql.ErrNoRows) {
		return nil, wrapDB(err)
	}

	now := time.Now().UTC()
	schemaRaw, _ := json.Marshal(schema)
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO resources(id, revision, live, schema_kv, created_at, updated_at) VALUES(?,1,?,?,?,?)`,
		id, string(normalize(live)), string(schemaRaw), now.Format(time.RFC3339Nano), now.Format(time.RFC3339Nano)); err != nil {
		return nil, wrapDB(err)
	}
	if manager != "" {
		liveV, _ := model.DecodeValue(live)
		for ps := range merge.Leaves(liveV, &schema) {
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO owners(resource_id, path, manager) VALUES(?,?,?)`, id, ps, manager); err != nil {
				return nil, wrapDB(err)
			}
		}
		if applied != nil {
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO applied(resource_id, manager, config, at) VALUES(?,?,?,?)`,
				id, manager, string(normalize(applied)), now.Format(time.RFC3339Nano)); err != nil {
				return nil, wrapDB(err)
			}
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, wrapDB(err)
	}
	return s.Snapshot(ctx, id)
}

func (s *SQLite) Snapshot(ctx context.Context, id string) (*Snapshot, error) {
	var (
		rev                                      int64
		liveRaw, schemaRaw, createdRaw, updatedR string
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT revision, live, schema_kv, created_at, updated_at FROM resources WHERE id = ?`, id).
		Scan(&rev, &liveRaw, &schemaRaw, &createdRaw, &updatedR)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, &model.Error{Category: model.CatNotFound, Code: "resource_not_found",
			Message: "resource " + id + " does not exist"}
	}
	if err != nil {
		return nil, wrapDB(err)
	}
	schema := model.Schema{}
	if err := json.Unmarshal([]byte(schemaRaw), &schema); err != nil {
		return nil, internal("schema decode", err)
	}
	created, _ := time.Parse(time.RFC3339Nano, createdRaw)
	updated, _ := time.Parse(time.RFC3339Nano, updatedR)

	owners := model.Owners{}
	rows, err := s.db.QueryContext(ctx, `SELECT path, manager FROM owners WHERE resource_id = ?`, id)
	if err != nil {
		return nil, wrapDB(err)
	}
	defer rows.Close()
	for rows.Next() {
		var path, mgr string
		if err := rows.Scan(&path, &mgr); err != nil {
			return nil, wrapDB(err)
		}
		owners.Add(path, mgr)
	}
	if err := rows.Err(); err != nil {
		return nil, wrapDB(err)
	}

	return &Snapshot{
		ID: id, Revision: rev, Live: json.RawMessage(liveRaw), Schema: schema, Owners: owners,
		CreatedAt: created, UpdatedAt: updated,
	}, nil
}

func (s *SQLite) AppliedOf(ctx context.Context, id, manager string) (json.RawMessage, bool, error) {
	var exists int
	if err := s.db.QueryRowContext(ctx, `SELECT 1 FROM resources WHERE id = ?`, id).Scan(&exists); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, false, &model.Error{Category: model.CatNotFound, Code: "resource_not_found",
				Message: "resource " + id + " does not exist"}
		}
		return nil, false, wrapDB(err)
	}
	var raw string
	err := s.db.QueryRowContext(ctx,
		`SELECT config FROM applied WHERE resource_id = ? AND manager = ?`, id, manager).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, wrapDB(err)
	}
	return json.RawMessage(raw), true, nil
}

func (s *SQLite) Commit(ctx context.Context, id string, c Commit) (int64, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, wrapDB(err)
	}
	defer rollback(tx)

	var curRev int64
	err = tx.QueryRowContext(ctx, `SELECT revision FROM resources WHERE id = ?`, id).Scan(&curRev)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, &model.Error{Category: model.CatNotFound, Code: "resource_not_found",
			Message: "resource " + id + " does not exist"}
	}
	if err != nil {
		return 0, wrapDB(err)
	}
	if c.BaseRev != curRev {
		return 0, &model.Error{Category: model.CatStateConflict, Code: "revision_stale",
			Message: fmt.Sprintf("expected revision %d but resource is at %d", c.BaseRev, curRev)}
	}

	nextRev := curRev + 1
	now := c.At.UTC().Format(time.RFC3339Nano)
	if _, err := tx.ExecContext(ctx,
		`UPDATE resources SET revision = ?, live = ?, updated_at = ? WHERE id = ?`,
		nextRev, string(normalize(c.Live)), now, id); err != nil {
		return 0, wrapDB(err)
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM owners WHERE resource_id = ?`, id); err != nil {
		return 0, wrapDB(err)
	}
	for path, ms := range c.Owners {
		for mgr := range ms {
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO owners(resource_id, path, manager) VALUES(?,?,?)`, id, path, mgr); err != nil {
				return 0, wrapDB(err)
			}
		}
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO applied(resource_id, manager, config, at) VALUES(?,?,?,?)
		 ON CONFLICT(resource_id, manager) DO UPDATE SET config = excluded.config, at = excluded.at`,
		id, c.Manager, string(normalize(c.Applied)), now); err != nil {
		return 0, wrapDB(err)
	}
	changesRaw, _ := json.Marshal(c.Changes)
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO history(resource_id, revision, run_id, manager, reason, forced, changes, at)
		 VALUES(?,?,?,?,?,?,?,?)`,
		id, nextRev, c.RunID, c.Manager, c.Reason, boolInt(c.Forced), string(changesRaw), now); err != nil {
		return 0, wrapDB(err)
	}
	if err := tx.Commit(); err != nil {
		return 0, wrapDB(err)
	}
	return nextRev, nil
}

func (s *SQLite) History(ctx context.Context, id string, limit int) ([]HistoryEntry, error) {
	var exists int
	if err := s.db.QueryRowContext(ctx, `SELECT 1 FROM resources WHERE id = ?`, id).Scan(&exists); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, &model.Error{Category: model.CatNotFound, Code: "resource_not_found",
				Message: "resource " + id + " does not exist"}
		}
		return nil, wrapDB(err)
	}
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT revision, run_id, manager, reason, forced, changes, at
		 FROM history WHERE resource_id = ? ORDER BY revision DESC LIMIT ?`, id, limit)
	if err != nil {
		return nil, wrapDB(err)
	}
	defer rows.Close()
	var out []HistoryEntry
	for rows.Next() {
		var (
			rev                int64
			runID, mgr, reason string
			forced             int
			changesRaw, atRaw  string
		)
		if err := rows.Scan(&rev, &runID, &mgr, &reason, &forced, &changesRaw, &atRaw); err != nil {
			return nil, wrapDB(err)
		}
		h := HistoryEntry{ResourceID: id, Revision: rev, RunID: runID, Manager: mgr,
			Reason: reason, Forced: forced != 0}
		if err := json.Unmarshal([]byte(changesRaw), &h.Changes); err != nil {
			return nil, internal("history decode", err)
		}
		h.At, _ = time.Parse(time.RFC3339Nano, atRaw)
		out = append(out, h)
	}
	if err := rows.Err(); err != nil {
		return nil, wrapDB(err)
	}
	return out, nil
}

func (s *SQLite) Close() error { return s.db.Close() }

// -----------------------------------------------------------------------------
// error mapping
// -----------------------------------------------------------------------------

// wrapDB maps driver errors to the structured error contract. Disk/IO
// exhaustion and lock timeouts are resource_exhausted; anything unexpected is
// computation_failure so callers never see a bare database string.
func wrapDB(err error) error {
	if err == nil {
		return nil
	}
	msg := err.Error()
	switch {
	case strings.Contains(msg, "SQLITE_FULL"),
		strings.Contains(msg, "SQLITE_IOERR"),
		strings.Contains(msg, "SQLITE_NOMEM"),
		strings.Contains(msg, "SQLITE_TOOBIG"),
		strings.Contains(msg, "too many levels"):
		return &model.Error{Category: model.CatResourceExhausted, Code: "storage_exhausted",
			Message: msg, Cause: err}
	case strings.Contains(msg, "SQLITE_BUSY"), strings.Contains(msg, "database is locked"):
		return &model.Error{Category: model.CatResourceExhausted, Code: "storage_busy",
			Message: "storage lock contention; retry", Cause: err}
	default:
		return internal("sqlite", err)
	}
}

func internal(where string, err error) *model.Error {
	return &model.Error{Category: model.CatComputationFailure, Code: "internal_error",
		Message: fmt.Sprintf("%s: %v", where, err), Cause: err}
}

func normalize(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return json.RawMessage("null")
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return raw
	}
	b, err := json.Marshal(v)
	if err != nil {
		return raw
	}
	return b
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func rollback(tx *sql.Tx) { _ = tx.Rollback() }
