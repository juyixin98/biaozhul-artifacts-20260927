// Package store provides the SQLite-backed implementation of nat.StateStore.
// The database file is the durable replay surface: runs, every mapping row and
// every decision event persist there. An in-memory DSN ("file::memory:") gives
// tests an isolated, throwaway store that still exercises the real SQL.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"natlab/internal/model"
)

// SQLiteStore persists NAT state in one SQLite database.
type SQLiteStore struct {
	db *sql.DB
}

// Open opens (creating the schema if needed) a store at path. The idiomatic
// pure-Go driver name is "sqlite".
func Open(ctx context.Context, path string) (*SQLiteStore, error) {
	// Append driver pragmas whether or not the path already carries a query
	// (e.g. a shared in-memory DSN "file:name?mode=memory&cache=shared").
	sep := "?"
	if strings.Contains(path, "?") {
		sep = "&"
	}
	dsn := path + sep + "_pragma=busy_timeout(5000)&_pragma=foreign_keys(on)&_time_format=sqlite"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// Single writer connection: removes SQLITE_BUSY races under concurrency and
	// keeps the per-run engine mutex as the concurrency story.
	db.SetMaxOpenConns(1)
	s := &SQLiteStore{db: db}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close closes the underlying database.
func (s *SQLiteStore) Close() error { return s.db.Close() }

// DB exposes the handle for health checks.
func (s *SQLiteStore) DB() *sql.DB { return s.db }

func (s *SQLiteStore) migrate(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, schema)
	return err
}

const schema = `
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    high_water  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mappings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    protocol     TEXT NOT NULL,
    src_ip       TEXT NOT NULL,
    src_port     INTEGER NOT NULL,
    dst_ip       TEXT NOT NULL,
    dst_port     INTEGER NOT NULL,
    mapped_port  INTEGER NOT NULL,
    state        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mappings_run ON mappings(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_flow ON mappings(run_id, protocol, src_ip, src_port, dst_ip, dst_port)
    WHERE state NOT IN ('closed');
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_port ON mappings(run_id, protocol, mapped_port)
    WHERE state NOT IN ('closed');
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    observed_at  TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    clock_rewind INTEGER NOT NULL,
    accepted     INTEGER NOT NULL,
    category     TEXT NOT NULL,
    code         TEXT NOT NULL,
    reason       TEXT NOT NULL,
    mapping_id   INTEGER NOT NULL,
    mapped_port  INTEGER NOT NULL,
    state        TEXT NOT NULL,
    packet_json  TEXT NOT NULL,
    detail       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
`

// EnsureRun implements nat.StateStore.
func (s *SQLiteStore) EnsureRun(ctx context.Context, runID string) (time.Time, error) {
	now := time.Now().UTC()
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO runs(run_id, created_at, high_water) VALUES(?, ?, '')
		 ON CONFLICT(run_id) DO NOTHING`,
		runID, ts(now)); err != nil {
		return time.Time{}, err
	}
	var hw string
	if err := s.db.QueryRowContext(ctx,
		`SELECT high_water FROM runs WHERE run_id = ?`, runID).Scan(&hw); err != nil {
		return time.Time{}, err
	}
	if hw == "" {
		return time.Time{}, nil
	}
	return parseTS(hw)
}

// SetClock implements nat.StateStore.
func (s *SQLiteStore) SetClock(ctx context.Context, runID string, now time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE runs SET high_water = ? WHERE run_id = ?`, ts(now), runID)
	return err
}

// SweepExpired implements nat.StateStore.
func (s *SQLiteStore) SweepExpired(ctx context.Context, runID string, now time.Time) ([]*model.Mapping, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, protocol, src_ip, src_port, dst_ip, dst_port, mapped_port,
		        state, created_at, last_used_at, expires_at
		 FROM mappings
		 WHERE run_id = ? AND state NOT IN ('closed') AND expires_at <= ?
		 ORDER BY id`, runID, ts(now))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Mapping
	var ids []int64
	for rows.Next() {
		m, err := scanMapping(rows, runID)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
		ids = append(ids, m.ID)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for _, id := range ids {
		if _, err := s.db.ExecContext(ctx,
			`UPDATE mappings SET state = 'closed' WHERE id = ? AND state NOT IN ('closed')`, id); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// ActiveByFlow implements nat.StateStore.
func (s *SQLiteStore) ActiveByFlow(ctx context.Context, runID string, k model.FlowKey, now time.Time) (*model.Mapping, error) {
	return s.queryOne(ctx,
		`SELECT id, protocol, src_ip, src_port, dst_ip, dst_port, mapped_port,
		        state, created_at, last_used_at, expires_at
		 FROM mappings
		 WHERE run_id = ? AND protocol = ? AND src_ip = ? AND src_port = ?
		   AND dst_ip = ? AND dst_port = ? AND state NOT IN ('closed') AND expires_at > ?
		 LIMIT 1`,
		runID, string(k.Protocol), k.SrcIP, k.SrcPort, k.DstIP, k.DstPort, ts(now))
}

// ActiveByExtPort implements nat.StateStore.
func (s *SQLiteStore) ActiveByExtPort(ctx context.Context, runID string, proto model.Protocol, mappedPort uint16, now time.Time) (*model.Mapping, error) {
	return s.queryOne(ctx,
		`SELECT id, protocol, src_ip, src_port, dst_ip, dst_port, mapped_port,
		        state, created_at, last_used_at, expires_at
		 FROM mappings
		 WHERE run_id = ? AND protocol = ? AND mapped_port = ?
		   AND state NOT IN ('closed') AND expires_at > ?
		 LIMIT 1`,
		runID, string(proto), mappedPort, ts(now))
}

// HistoryByExtPort implements nat.StateStore.
func (s *SQLiteStore) HistoryByExtPort(ctx context.Context, runID string, proto model.Protocol, mappedPort uint16) (*model.Mapping, error) {
	return s.queryOne(ctx,
		`SELECT id, protocol, src_ip, src_port, dst_ip, dst_port, mapped_port,
		        state, created_at, last_used_at, expires_at
		 FROM mappings
		 WHERE run_id = ? AND protocol = ? AND mapped_port = ?
		 ORDER BY id DESC LIMIT 1`,
		runID, string(proto), mappedPort)
}

// InsertMapping implements nat.StateStore.
func (s *SQLiteStore) InsertMapping(ctx context.Context, runID string, m *model.Mapping) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO mappings(run_id, protocol, src_ip, src_port, dst_ip, dst_port,
		       mapped_port, state, created_at, last_used_at, expires_at)
		 VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		runID, string(m.Protocol), m.SrcIP, m.SrcPort, m.DstIP, m.DstPort,
		m.MappedPort, m.State, ts(m.CreatedAt), ts(m.LastUsedAt), ts(m.ExpiresAt))
	if err != nil {
		return 0, err
	}
	id, err := res.LastInsertId()
	if err != nil {
		return 0, err
	}
	m.ID = id
	return id, nil
}

// UpdateMapping implements nat.StateStore.
func (s *SQLiteStore) UpdateMapping(ctx context.Context, runID string, m *model.Mapping) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE mappings SET state = ?, last_used_at = ?, expires_at = ? WHERE id = ? AND run_id = ?`,
		m.State, ts(m.LastUsedAt), ts(m.ExpiresAt), m.ID, runID)
	return err
}

// CloseMapping implements nat.StateStore.
func (s *SQLiteStore) CloseMapping(ctx context.Context, runID string, id int64, now time.Time) (bool, error) {
	res, err := s.db.ExecContext(ctx,
		`UPDATE mappings SET state = 'closed', last_used_at = ? WHERE id = ? AND run_id = ? AND state NOT IN ('closed')`,
		ts(now), id, runID)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n > 0, err
}

// CountActive implements nat.StateStore.
func (s *SQLiteStore) CountActive(ctx context.Context, runID string, now time.Time) (int, error) {
	var n int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM mappings
		 WHERE run_id = ? AND state NOT IN ('closed') AND expires_at > ?`,
		runID, ts(now)).Scan(&n)
	return n, err
}

// ListMappings implements nat.StateStore.
func (s *SQLiteStore) ListMappings(ctx context.Context, runID string, activeOnly bool) ([]*model.Mapping, error) {
	q := `SELECT id, protocol, src_ip, src_port, dst_ip, dst_port, mapped_port,
	             state, created_at, last_used_at, expires_at
	      FROM mappings WHERE run_id = ?`
	if activeOnly {
		q += ` AND state NOT IN ('closed')`
	}
	q += ` ORDER BY id`
	rows, err := s.db.QueryContext(ctx, q, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Mapping
	for rows.Next() {
		m, err := scanMapping(rows, runID)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// AppendEvent implements nat.StateStore.
func (s *SQLiteStore) AppendEvent(ctx context.Context, e *model.Event) error {
	pktJSON, err := json.Marshal(e.Packet)
	if err != nil {
		return err
	}
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO events(run_id, seq, observed_at, effective_at, clock_rewind,
		       accepted, category, code, reason, mapping_id, mapped_port, state,
		       packet_json, detail)
		 VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		e.RunID, e.Seq, ts(e.ObservedAt), ts(e.EffectiveAt), boolInt(e.ClockRewind),
		boolInt(e.Accepted), string(e.Category), e.Code, e.Reason,
		e.MappingID, e.MappedPort, e.State, string(pktJSON), e.Detail)
	if err != nil {
		return err
	}
	id, err := res.LastInsertId()
	if err == nil {
		e.ID = id
	}
	return err
}

// ListEvents implements nat.StateStore.
func (s *SQLiteStore) ListEvents(ctx context.Context, runID string, limit int) ([]*model.Event, error) {
	q := `SELECT id, run_id, seq, observed_at, effective_at, clock_rewind, accepted,
	             category, code, reason, mapping_id, mapped_port, state, packet_json, detail
	      FROM events WHERE run_id = ? ORDER BY id`
	args := []any{runID}
	if limit > 0 {
		q += ` LIMIT ?`
		args = append(args, limit)
	}
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Event
	for rows.Next() {
		var ev model.Event
		var obsStr, effStr, pktJSON string
		var accepted, rewind int
		var mappingID int64
		var mappedPort int
		if err := rows.Scan(&ev.ID, &ev.RunID, &ev.Seq, &obsStr, &effStr, &rewind, &accepted,
			&ev.Category, &ev.Code, &ev.Reason, &mappingID, &mappedPort, &ev.State,
			&pktJSON, &ev.Detail); err != nil {
			return nil, err
		}
		ev.ObservedAt, _ = parseTS(obsStr)
		ev.EffectiveAt, _ = parseTS(effStr)
		ev.ClockRewind = rewind != 0
		ev.Accepted = accepted != 0
		ev.MappingID = mappingID
		ev.MappedPort = uint16(mappedPort)
		if err := json.Unmarshal([]byte(pktJSON), &ev.Packet); err != nil {
			return nil, fmt.Errorf("event %d packet_json: %w", ev.ID, err)
		}
		out = append(out, &ev)
	}
	return out, rows.Err()
}

// rowScanner abstracts *sql.Row / *sql.Rows.
type rowScanner interface {
	Scan(dest ...any) error
}

func queryScan(r rowScanner, runID string) (*model.Mapping, error) {
	m := &model.Mapping{RunID: runID}
	var proto, created, lastUsed, expires string
	if err := r.Scan(&m.ID, &proto, &m.SrcIP, &m.SrcPort, &m.DstIP, &m.DstPort,
		&m.MappedPort, &m.State, &created, &lastUsed, &expires); err != nil {
		return nil, err
	}
	m.Protocol = model.Protocol(proto)
	m.CreatedAt, _ = parseTS(created)
	m.LastUsedAt, _ = parseTS(lastUsed)
	m.ExpiresAt, _ = parseTS(expires)
	return m, nil
}

func (s *SQLiteStore) queryOne(ctx context.Context, q string, args ...any) (*model.Mapping, error) {
	row := s.db.QueryRowContext(ctx, q, args...)
	m, err := queryScan(row, args[0].(string))
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return m, nil
}

func scanMapping(rows *sql.Rows, runID string) (*model.Mapping, error) {
	return queryScan(rows, runID)
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// ts formats time for the sqlite driver's text columns.
func ts(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.UTC().Format("2006-01-02 15:04:05.999999999-07:00")
}

func parseTS(s string) (time.Time, error) {
	if s == "" {
		return time.Time{}, nil
	}
	layouts := []string{
		"2006-01-02 15:04:05.999999999-07:00",
		"2006-01-02 15:04:05.999999999Z07:00",
		"2006-01-02T15:04:05.999999999Z07:00",
		"2006-01-02T15:04:05Z07:00",
	}
	var lastErr error
	for _, l := range layouts {
		if t, err := time.Parse(l, s); err == nil {
			return t, nil
		} else {
			lastErr = err
		}
	}
	return time.Time{}, fmt.Errorf("parse timestamp %q: %w", s, lastErr)
}
