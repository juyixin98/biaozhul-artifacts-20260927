// Package store persists configuration versions, analysis reports and
// replay request logs in SQLite. Every API response can be correlated with
// (a) the config version it ran against, (b) the decision steps taken, and
// (c) where the result was produced (server instance + source location).
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "modernc.org/sqlite"
)

// Store wraps the SQLite database.
type Store struct {
	db       *sql.DB
	instance string
}

// Open opens (creating the schema in) the SQLite database at dsn.
// instance labels the producing server in every log row.
func Open(ctx context.Context, dsn, instance string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1) // SQLite: avoid "database is locked" under local demo load
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	s := &Store{db: db, instance: instance}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) migrate(ctx context.Context) error {
	pragmas := []string{
		`PRAGMA journal_mode=WAL`,
		`PRAGMA synchronous=NORMAL`,
		`PRAGMA foreign_keys=ON`,
	}
	for _, q := range pragmas {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("pragma: %w", err)
		}
	}
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS config_versions (
			version INTEGER PRIMARY KEY AUTOINCREMENT,
			raw     TEXT NOT NULL,
			parse_ok INTEGER NOT NULL,
			parse_errors TEXT NOT NULL,
			created_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS reports (
			version INTEGER PRIMARY KEY,
			payload TEXT NOT NULL,
			created_at TEXT NOT NULL,
			FOREIGN KEY(version) REFERENCES config_versions(version)
		)`,
		`CREATE TABLE IF NOT EXISTS request_logs (
			request_id TEXT PRIMARY KEY,
			version INTEGER,
			family TEXT,
			packet TEXT NOT NULL,
			decision TEXT NOT NULL,
			matched_rule_id TEXT,
			steps_json TEXT NOT NULL,
			certain INTEGER NOT NULL,
			uncertainties TEXT NOT NULL,
			errors TEXT NOT NULL DEFAULT '[]',
			instance TEXT NOT NULL,
			source_location TEXT NOT NULL,
			created_at TEXT NOT NULL
		)`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("migrate: %w", err)
		}
	}
	return nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// SaveConfig records a configuration version and its parse outcome.
func (s *Store) SaveConfig(ctx context.Context, raw string, parseOK bool, parseErrorsJSON []byte) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO config_versions(raw, parse_ok, parse_errors, created_at) VALUES(?,?,?,?)`,
		raw, btoi(parseOK), string(parseErrorsJSON), time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

// SaveReport stores the analysis report JSON for a version.
func (s *Store) SaveReport(ctx context.Context, version int64, payload []byte) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT OR REPLACE INTO reports(version, payload, created_at) VALUES(?,?,?)`,
		version, string(payload), time.Now().UTC().Format(time.RFC3339Nano))
	return err
}

// Report loads a stored report.
func (s *Store) Report(ctx context.Context, version int64) ([]byte, error) {
	var payload string
	err := s.db.QueryRowContext(ctx, `SELECT payload FROM reports WHERE version=?`, version).Scan(&payload)
	if err != nil {
		return nil, err
	}
	return []byte(payload), nil
}

// LatestVersion returns the highest config version, or 0 if none.
func (s *Store) LatestVersion(ctx context.Context) (int64, error) {
	var v int64
	err := s.db.QueryRowContext(ctx, `SELECT COALESCE(MAX(version),0) FROM config_versions`).Scan(&v)
	return v, err
}

// RawConfig returns the raw JSON stored for a version.
func (s *Store) RawConfig(ctx context.Context, version int64) (string, error) {
	var raw string
	err := s.db.QueryRowContext(ctx, `SELECT raw FROM config_versions WHERE version=?`, version).Scan(&raw)
	return raw, err
}

// Step is one explainable evaluation step for a packet.
type Step struct {
	Order       int    `json:"order"`
	RuleID      string `json:"rule_id"`
	Matched     bool   `json:"matched"`
	Action      string `json:"action,omitempty"`
	Explanation string `json:"explanation"`
}

// LogEntry is one correlated request record.
type LogEntry struct {
	RequestID      string
	Version        int64
	Family         string
	PacketJSON     []byte
	Decision       string
	MatchedRuleID  string
	Steps          []Step
	Certain        bool
	Uncertainties  []string
	Errors         []string
	SourceLocation string
}

// LogRequest writes one request-log row.
func (s *Store) LogRequest(ctx context.Context, e LogEntry) error {
	steps, err := json.Marshal(e.Steps)
	if err != nil {
		return err
	}
	unc, err := json.Marshal(e.Uncertainties)
	if err != nil {
		return err
	}
	errs, err := json.Marshal(e.Errors)
	if err != nil {
		return err
	}
	matched := sql.NullString{String: e.MatchedRuleID, Valid: e.MatchedRuleID != ""}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO request_logs(request_id, version, family, packet, decision, matched_rule_id,
			steps_json, certain, uncertainties, errors, instance, source_location, created_at)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		e.RequestID, e.Version, e.Family, string(e.PacketJSON), e.Decision, matched,
		string(steps), btoi(e.Certain), string(unc), string(errs), s.instance, e.SourceLocation,
		time.Now().UTC().Format(time.RFC3339Nano))
	return err
}

// RequestLog is the persisted row as returned to clients.
type RequestLog struct {
	RequestID      string   `json:"request_id"`
	Version        int64    `json:"version"`
	Family         string   `json:"family"`
	Packet         string   `json:"packet"`
	Decision       string   `json:"decision"`
	MatchedRuleID  string   `json:"matched_rule_id,omitempty"`
	Steps          []Step   `json:"steps"`
	Certain        bool     `json:"certain"`
	Uncertainties  []string `json:"uncertainties"`
	Errors         []string `json:"errors"`
	Instance       string   `json:"instance"`
	SourceLocation string   `json:"source_location"`
	CreatedAt      string   `json:"created_at"`
}

// GetRequest fetches one correlated log row by request id.
func (s *Store) GetRequest(ctx context.Context, id string) (*RequestLog, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT request_id, version, family, packet, decision, COALESCE(matched_rule_id,''),
			steps_json, certain, uncertainties, errors, instance, source_location, created_at
		 FROM request_logs WHERE request_id=?`, id)
	var rl RequestLog
	var packet, stepsJSON, uncertainties, errors string
	var certain int
	if err := row.Scan(&rl.RequestID, &rl.Version, &rl.Family, &packet, &rl.Decision,
		&rl.MatchedRuleID, &stepsJSON, &certain, &uncertainties, &errors, &rl.Instance,
		&rl.SourceLocation, &rl.CreatedAt); err != nil {
		return nil, err
	}
	rl.Packet = packet
	if err := json.Unmarshal([]byte(stepsJSON), &rl.Steps); err != nil {
		return nil, err
	}
	rl.Certain = certain != 0
	if err := json.Unmarshal([]byte(uncertainties), &rl.Uncertainties); err != nil {
		return nil, err
	}
	if err := json.Unmarshal([]byte(errors), &rl.Errors); err != nil {
		rl.Errors = nil
	}
	return &rl, nil
}

func btoi(b bool) int {
	if b {
		return 1
	}
	return 0
}
