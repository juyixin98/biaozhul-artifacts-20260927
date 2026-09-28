// Package store persists the event journal and derived membership state in
// SQLite (pure-Go modernc driver, no CGO). The state core itself is
// storage-agnostic; the store exists so a replay can be reconstructed from
// the journal and inspected offline.
package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite"

	"igmpv2timer/internal/model"
)

// Store is the SQLite-backed event/state journal.
type Store struct {
	db *sql.DB
}

// Open opens (creating the file and schema) a SQLite database at path.
// Use ":memory:" for an ephemeral database.
func Open(path string) (*Store, error) {
	if path != ":memory:" {
		if dir := filepath.Dir(path); dir != "" && dir != "." {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				return nil, fmt.Errorf("create db directory: %w", err)
			}
		}
	}
	// _pragma options apply to every connection.
	dsn := path + "?_pragma=busy_timeout(5000)&_pragma=foreign_keys(on)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1) // serializes writes; deterministic journaling
	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) migrate() error {
	const schema = `
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY,
    at_ms        INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    iface        TEXT NOT NULL,
    grp          TEXT NOT NULL DEFAULT '',
    member       TEXT NOT NULL DEFAULT '',
    source_addr  TEXT NOT NULL DEFAULT '',
    response_to  TEXT NOT NULL DEFAULT '',
    request_id   TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnostics (
    seq         INTEGER PRIMARY KEY,
    at_ms       INTEGER NOT NULL,
    iface       TEXT NOT NULL,
    grp         TEXT NOT NULL DEFAULT '',
    member      TEXT NOT NULL DEFAULT '',
    packet      TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    request_id  TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emitted (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms      INTEGER NOT NULL,
    iface      TEXT NOT NULL,
    grp        TEXT NOT NULL DEFAULT '',
    packet     TEXT NOT NULL,
    gen        INTEGER NOT NULL,
    request_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS intervals (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    iface  TEXT NOT NULL,
    grp    TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE(iface, grp, start_ms)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL
);
`
	_, err := s.db.Exec(schema)
	if err != nil {
		return fmt.Errorf("migrate schema: %w", err)
	}
	return nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// BeginRun records a replay run and its (redaction-free, local fixture)
// configuration.
func (s *Store) BeginRun(name string, cfgJSON []byte) (int64, error) {
	res, err := s.db.Exec(
		`INSERT INTO runs(started_at, name, config_json) VALUES(?,?,?)`,
		time.Now().UTC().Format(time.RFC3339Nano), name, string(cfgJSON))
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

// AppendEvent journals an input event.
func (s *Store) AppendEvent(ev model.Event) error {
	raw, _ := json.Marshal(ev)
	_, err := s.db.Exec(
		`INSERT INTO events(seq, at_ms, kind, iface, grp, member, source_addr,
		    response_to, request_id, payload_json)
		 VALUES(?,?,?,?,?,?,?,?,?,?)`,
		ev.Seq, int64(ev.At), string(ev.Kind), ev.Iface, ev.Group, ev.Member,
		ev.SourceAddr, ev.ResponseTo, ev.RequestID, string(raw))
	if err != nil {
		return fmt.Errorf("append event seq=%d: %w", ev.Seq, err)
	}
	return nil
}

// AppendDiag journals a diagnostic record.
func (s *Store) AppendDiag(d model.Diag) error {
	raw, _ := json.Marshal(d)
	_, err := s.db.Exec(
		`INSERT INTO diagnostics(seq, at_ms, iface, grp, member, packet, verdict,
		    reason, request_id, payload_json)
		 VALUES(?,?,?,?,?,?,?,?,?,?)`,
		d.Seq, int64(d.At), d.Iface, d.Group, d.Member, string(d.Packet),
		string(d.Verdict), d.Reason, d.RequestID, string(raw))
	return err
}

// AppendEmitted journals an emitted query packet.
func (s *Store) AppendEmitted(p model.EmittedPkt) error {
	_, err := s.db.Exec(
		`INSERT INTO emitted(at_ms, iface, grp, packet, gen, request_id)
		 VALUES(?,?,?,?,?,?)`,
		int64(p.At), p.Iface, p.Group, string(p.Packet), p.Gen, p.RequestID)
	return err
}

// UpsertInterval records a forwarding-entry retention interval (end_ms=0
// while open).
func (s *Store) UpsertInterval(iv model.Interval) error {
	_, err := s.db.Exec(
		`INSERT INTO intervals(iface, grp, start_ms, end_ms, reason)
		 VALUES(?,?,?,?,?)
		 ON CONFLICT(iface, grp, start_ms) DO UPDATE SET
		   end_ms=excluded.end_ms, reason=excluded.reason`,
		iv.Iface, iv.Group, int64(iv.Start), int64(iv.End), iv.Reason)
	return err
}

// Events returns all journaled input events in journal order.
func (s *Store) Events() ([]model.Event, error) {
	rows, err := s.db.Query(
		`SELECT payload_json FROM events ORDER BY seq ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Event
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var ev model.Event
		if err := json.Unmarshal([]byte(raw), &ev); err != nil {
			return nil, err
		}
		out = append(out, ev)
	}
	return out, rows.Err()
}

// Diagnostics returns all diagnostics in journal order.
func (s *Store) Diagnostics() ([]model.Diag, error) {
	rows, err := s.db.Query(
		`SELECT payload_json FROM diagnostics ORDER BY seq ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Diag
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var d model.Diag
		if err := json.Unmarshal([]byte(raw), &d); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// Emitted returns emitted packets in time/id order.
func (s *Store) Emitted() ([]model.EmittedPkt, error) {
	rows, err := s.db.Query(
		`SELECT at_ms, iface, grp, packet, gen, request_id
		 FROM emitted ORDER BY at_ms ASC, id ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.EmittedPkt
	for rows.Next() {
		var p model.EmittedPkt
		var pkt string
		if err := rows.Scan(&p.At, &p.Iface, &p.Group, &pkt, &p.Gen, &p.RequestID); err != nil {
			return nil, err
		}
		p.Packet = model.PacketType(pkt)
		out = append(out, p)
	}
	return out, rows.Err()
}

// Intervals returns all retention intervals in start order.
func (s *Store) Intervals() ([]model.Interval, error) {
	rows, err := s.db.Query(
		`SELECT iface, grp, start_ms, end_ms, reason
		 FROM intervals ORDER BY start_ms ASC, id ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Interval
	for rows.Next() {
		var iv model.Interval
		if err := rows.Scan(&iv.Iface, &iv.Group, &iv.Start, &iv.End, &iv.Reason); err != nil {
			return nil, err
		}
		out = append(out, iv)
	}
	return out, rows.Err()
}

// Reset clears all tables (used between test replays).
func (s *Store) Reset() error {
	_, err := s.db.Exec(`
DELETE FROM events; DELETE FROM diagnostics; DELETE FROM emitted;
DELETE FROM intervals; DELETE FROM runs;`)
	return err
}
