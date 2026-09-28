// Package store persists policy versions, analysis reports and replay
// decisions in SQLite. Everything needed to explain an answer — which policy
// version was evaluated, which rule position decided it, which witnesses were
// produced — is stored and retrievable by request id.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"fwrule/internal/analyzer"
	"fwrule/internal/config"

	_ "modernc.org/sqlite"
)

// Store is the SQLite-backed state store.
type Store struct {
	db *sql.DB
}

// PolicyVersion is one stored configuration revision.
type PolicyVersion struct {
	Version   int64
	Name      string
	Source    string // original JSON text
	CreatedAt time.Time
}

// Open opens (creating if needed) the database at dsn and runs migrations.
func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// Serialize writes; modernc.org/sqlite likes a single writer connection.
	db.SetMaxOpenConns(1)
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS schema_meta (
			key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS policy_versions (
			version INTEGER PRIMARY KEY AUTOINCREMENT,
			name TEXT NOT NULL,
			source TEXT NOT NULL,
			created_at TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS analysis_reports (
			version INTEGER PRIMARY KEY,
			report_json TEXT NOT NULL,
			analyzed_at TEXT NOT NULL,
			FOREIGN KEY(version) REFERENCES policy_versions(version))`,
		`CREATE TABLE IF NOT EXISTS replay_log (
			request_id TEXT PRIMARY KEY,
			version INTEGER NOT NULL,
			policy_name TEXT NOT NULL,
			request_json TEXT NOT NULL,
			matched_rule_id TEXT,
			matched_rule_index INTEGER,
			action TEXT NOT NULL,
			decided_by TEXT NOT NULL,
			status TEXT NOT NULL,
			error_code TEXT,
			error_detail TEXT,
			trace_json TEXT NOT NULL,
			created_at TEXT NOT NULL)`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("migration failed: %w", err)
		}
	}
	return nil
}

// SavePolicy validates raw, stores it as a new version and returns the
// version. Validation here means a bad file can never become a stored
// revision.
func (s *Store) SavePolicy(ctx context.Context, name string, raw []byte) (*PolicyVersion, *config.Policy, error) {
	pol, err := config.Load(raw, name)
	if err != nil {
		return nil, nil, err
	}
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO policy_versions(name, source, created_at) VALUES(?,?,?)`,
		pol.Name, string(raw), time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		return nil, nil, err
	}
	v, _ := res.LastInsertId()
	pv := &PolicyVersion{
		Version: v, Name: pol.Name, Source: string(raw),
		CreatedAt: time.Now().UTC(),
	}
	return pv, pol, nil
}

// SaveReport stores the analysis report for a version.
func (s *Store) SaveReport(ctx context.Context, version int64, rep *analyzer.Report) error {
	b, err := json.Marshal(rep)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO analysis_reports(version, report_json, analyzed_at) VALUES(?,?,?)
		 ON CONFLICT(version) DO UPDATE SET report_json=excluded.report_json,
		   analyzed_at=excluded.analyzed_at`,
		version, string(b), time.Now().UTC().Format(time.RFC3339Nano))
	return err
}

// LatestVersion returns the newest stored policy version.
func (s *Store) LatestVersion(ctx context.Context) (*PolicyVersion, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT version, name, source, created_at FROM policy_versions
		 ORDER BY version DESC LIMIT 1`)
	var pv PolicyVersion
	var created string
	if err := row.Scan(&pv.Version, &pv.Name, &pv.Source, &created); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNoPolicy
		}
		return nil, err
	}
	pv.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	return &pv, nil
}

// GetVersion returns one stored version (1-based; 0 is treated as latest by
// callers via ResolveVersion).
func (s *Store) GetVersion(ctx context.Context, version int64) (*PolicyVersion, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT version, name, source, created_at FROM policy_versions WHERE version=?`,
		version)
	var pv PolicyVersion
	var created string
	if err := row.Scan(&pv.Version, &pv.Name, &pv.Source, &created); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrVersionNotFound
		}
		return nil, err
	}
	pv.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	return &pv, nil
}

// ResolveVersion maps 0 (unspecified) to the latest version.
func (s *Store) ResolveVersion(ctx context.Context, version int64) (int64, error) {
	if version > 0 {
		if _, err := s.GetVersion(ctx, version); err != nil {
			return 0, err
		}
		return version, nil
	}
	pv, err := s.LatestVersion(ctx)
	if err != nil {
		return 0, err
	}
	return pv.Version, nil
}

// GetReport returns the stored analysis report for a version.
func (s *Store) GetReport(ctx context.Context, version int64) (*analyzer.Report, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT report_json FROM analysis_reports WHERE version=?`, version)
	var raw string
	if err := row.Scan(&raw); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNoReport
		}
		return nil, err
	}
	var rep analyzer.Report
	if err := json.Unmarshal([]byte(raw), &rep); err != nil {
		return nil, err
	}
	return &rep, nil
}

// ListVersions returns stored versions newest-first.
func (s *Store) ListVersions(ctx context.Context, limit int) ([]PolicyVersion, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT version, name, source, created_at FROM policy_versions
		 ORDER BY version DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PolicyVersion
	for rows.Next() {
		var pv PolicyVersion
		var created string
		if err := rows.Scan(&pv.Version, &pv.Name, &pv.Source, &created); err != nil {
			return nil, err
		}
		pv.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
		out = append(out, pv)
	}
	return out, rows.Err()
}

// Errors returned by the store.
var (
	ErrNoPolicy         = errors.New("no policy version stored yet")
	ErrVersionNotFound  = errors.New("policy version not found")
	ErrNoReport         = errors.New("no analysis report for that version")
	ErrDuplicateRequest = errors.New("duplicate request_id")
	ErrLogNotFound      = errors.New("replay log not found")
)
