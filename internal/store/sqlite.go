// Package store persists reassembly state and the audit trail in SQLite.
//
// The hot reassembly path keeps an in-memory index (see package reasm);
// SQLite holds the durable mirror of buffered fragments, completed
// datagrams and the append-only event log. Dropped groups delete their
// fragment rows, which is what resource-reclamation tests assert on.
package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"ipreasm/internal/reasm"
)

// EventRow is one audit-log row.
type EventRow struct {
	Seq      int64     `json:"seq"`
	TS       time.Time `json:"ts"`
	Category string    `json:"category"`
	Src      string    `json:"src"`
	Dst      string    `json:"dst"`
	Proto    int       `json:"proto"`
	Ident    int       `json:"ident"`
	Detail   string    `json:"detail"`
}

// DatagramRow is one completed datagram row.
type DatagramRow struct {
	Src       string    `json:"src"`
	Dst       string    `json:"dst"`
	Proto     int       `json:"proto"`
	Ident     int       `json:"ident"`
	TotalLen  int       `json:"total_len"`
	FragCount int       `json:"frag_count"`
	SHA256    string    `json:"sha256"`
	Payload   []byte    `json:"-"`
	Completed time.Time `json:"completed_at"`
}

// SQLiteStore implements reasm.Sink and run-scoped query helpers.
type SQLiteStore struct {
	db   *sql.DB
	path string
}

// Open opens (creating if needed) the SQLite file and applies the schema.
func Open(path string) (*SQLiteStore, error) {
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)")
	if err != nil {
		return nil, fmt.Errorf("open sqlite %s: %w", path, err)
	}
	db.SetMaxOpenConns(1) // serialise; the replay engine is single-threaded
	s := &SQLiteStore{db: db, path: path}
	if err := s.init(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLiteStore) init() error {
	pragmas := []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA synchronous=NORMAL",
	}
	for _, p := range pragmas {
		if _, err := s.db.Exec(p); err != nil {
			return fmt.Errorf("sqlite pragma: %w", err)
		}
	}
	_, err := s.db.Exec(schemaSQL)
	if err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	return nil
}

const schemaSQL = `
CREATE TABLE IF NOT EXISTS fragments (
  run_id       TEXT NOT NULL,
  src          TEXT NOT NULL,
  dst          TEXT NOT NULL,
  proto        INTEGER NOT NULL,
  ident        INTEGER NOT NULL,
  offset_bytes INTEGER NOT NULL,
  length       INTEGER NOT NULL,
  more         INTEGER NOT NULL,
  payload      BLOB NOT NULL,
  seen_at      TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  PRIMARY KEY (run_id, src, dst, proto, ident, offset_bytes)
);
CREATE INDEX IF NOT EXISTS idx_fragments_run ON fragments(run_id);

CREATE TABLE IF NOT EXISTS datagrams (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL,
  src          TEXT NOT NULL,
  dst          TEXT NOT NULL,
  proto        INTEGER NOT NULL,
  ident        INTEGER NOT NULL,
  total_len    INTEGER NOT NULL,
  frag_count   INTEGER NOT NULL,
  sha256       TEXT NOT NULL,
  payload      BLOB NOT NULL,
  completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_datagrams_run ON datagrams(run_id);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    TEXT NOT NULL,
  seq       INTEGER NOT NULL,
  ts        TEXT NOT NULL,
  category  TEXT NOT NULL,
  src       TEXT NOT NULL,
  dst       TEXT NOT NULL,
  proto     INTEGER NOT NULL,
  ident     INTEGER NOT NULL,
  detail    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
`

// Close releases the database handle.
func (s *SQLiteStore) Close() error { return s.db.Close() }

func keyParts(k reasm.Key) (string, string, int, int) {
	return k.Src.String(), k.Dst.String(), int(k.Proto), int(k.ID)
}

// OnFragmentStored inserts one accepted fragment and its stored event.
func (s *SQLiteStore) OnFragmentStored(ev reasm.Event, f reasm.Fragment) {
	src, dst, proto, ident := keyParts(f.Key)
	tx, err := s.db.Begin()
	if err != nil {
		panic(fmt.Sprintf("sqlite begin: %v", err))
	}
	if _, err := tx.Exec(`INSERT INTO fragments
		(run_id,src,dst,proto,ident,offset_bytes,length,more,payload,seen_at,seq)
		VALUES(?,?,?,?,?,?,?,?,?,?,?)`,
		ev.RunID, src, dst, proto, ident, f.OffsetBytes, len(f.Data), boolInt(f.More), f.Data,
		ev.TS.UTC().Format(time.RFC3339Nano), ev.Seq); err != nil {
		_ = tx.Rollback()
		panic(fmt.Sprintf("sqlite insert fragment: %v", err))
	}
	if _, err := tx.Exec(`INSERT INTO events
		(run_id,seq,ts,category,src,dst,proto,ident,detail)
		VALUES(?,?,?,?,?,?,?,?,?)`,
		ev.RunID, ev.Seq, ev.TS.UTC().Format(time.RFC3339Nano), ev.Category,
		src, dst, proto, ident, ev.Detail); err != nil {
		_ = tx.Rollback()
		panic(fmt.Sprintf("sqlite insert stored-event: %v", err))
	}
	if err := tx.Commit(); err != nil {
		panic(fmt.Sprintf("sqlite commit: %v", err))
	}
}

// OnGroupDropped deletes all buffered fragments of a group (completion,
// rejection, or expiry).
func (s *SQLiteStore) OnGroupDropped(ev reasm.Event, k reasm.Key) {
	src, dst, proto, ident := keyParts(k)
	_, err := s.db.Exec(`DELETE FROM fragments
		WHERE run_id=? AND src=? AND dst=? AND proto=? AND ident=?`,
		ev.RunID, src, dst, proto, ident)
	if err != nil {
		panic(fmt.Sprintf("sqlite drop group: %v", err))
	}
}

// OnDatagram persists the reassembled payload together with its digest
// and the completed audit event.
func (s *SQLiteStore) OnDatagram(ev reasm.Event, d reasm.Datagram) {
	sum := sha256.Sum256(d.Data)
	src, dst, proto, ident := keyParts(d.Key)
	tx, err := s.db.Begin()
	if err != nil {
		panic(fmt.Sprintf("sqlite begin: %v", err))
	}
	if _, err := tx.Exec(`INSERT INTO datagrams
		(run_id,src,dst,proto,ident,total_len,frag_count,sha256,payload,completed_at)
		VALUES(?,?,?,?,?,?,?,?,?,?)`,
		ev.RunID, src, dst, proto, ident, len(d.Data), d.FragCount,
		hex.EncodeToString(sum[:]), d.Data, d.Completed.UTC().Format(time.RFC3339Nano)); err != nil {
		_ = tx.Rollback()
		panic(fmt.Sprintf("sqlite insert datagram: %v", err))
	}
	if _, err := tx.Exec(`INSERT INTO events
		(run_id,seq,ts,category,src,dst,proto,ident,detail)
		VALUES(?,?,?,?,?,?,?,?,?)`,
		ev.RunID, ev.Seq, ev.TS.UTC().Format(time.RFC3339Nano), ev.Category,
		src, dst, proto, ident, ev.Detail); err != nil {
		_ = tx.Rollback()
		panic(fmt.Sprintf("sqlite insert completed-event: %v", err))
	}
	if err := tx.Commit(); err != nil {
		panic(fmt.Sprintf("sqlite commit: %v", err))
	}
}

// OnEvent appends one audit event.
func (s *SQLiteStore) OnEvent(ev reasm.Event) {
	src, dst, proto, ident := keyParts(ev.Key)
	_, err := s.db.Exec(`INSERT INTO events
		(run_id,seq,ts,category,src,dst,proto,ident,detail)
		VALUES(?,?,?,?,?,?,?,?,?)`,
		ev.RunID, ev.Seq, ev.TS.UTC().Format(time.RFC3339Nano), ev.Category,
		src, dst, proto, ident, ev.Detail)
	if err != nil {
		panic(fmt.Sprintf("sqlite insert event: %v", err))
	}
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// --- run-scoped queries used by the replay server and the tests ---------

// FragCount returns the number of buffered fragment rows for a run.
func (s *SQLiteStore) FragCount(runID string) (int, error) {
	var n int
	err := s.db.QueryRow(`SELECT count(*) FROM fragments WHERE run_id=?`, runID).Scan(&n)
	return n, err
}

// GroupFragCount returns buffered fragment rows for one group.
func (s *SQLiteStore) GroupFragCount(runID string, k reasm.Key) (int, error) {
	src, dst, proto, ident := keyParts(k)
	var n int
	err := s.db.QueryRow(`SELECT count(*) FROM fragments
		WHERE run_id=? AND src=? AND dst=? AND proto=? AND ident=?`,
		runID, src, dst, proto, ident).Scan(&n)
	return n, err
}

// EventCount returns the number of audit events of a given category.
func (s *SQLiteStore) EventCount(runID, category string) (int, error) {
	var n int
	err := s.db.QueryRow(`SELECT count(*) FROM events WHERE run_id=? AND category=?`, runID, category).Scan(&n)
	return n, err
}

// Events returns the audit events of a run in sequence order.
func (s *SQLiteStore) Events(runID string) ([]EventRow, error) {
	rows, err := s.db.Query(`SELECT seq,ts,category,src,dst,proto,ident,detail
		FROM events WHERE run_id=? ORDER BY seq,id`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanEvents(rows)
}

// Datagrams returns completed datagrams of a run in completion order.
func (s *SQLiteStore) Datagrams(runID string) ([]DatagramRow, error) {
	rows, err := s.db.Query(`SELECT src,dst,proto,ident,total_len,frag_count,sha256,payload,completed_at
		FROM datagrams WHERE run_id=? ORDER BY id`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DatagramRow
	for rows.Next() {
		var d DatagramRow
		var ts string
		if err := rows.Scan(&d.Src, &d.Dst, &d.Proto, &d.Ident, &d.TotalLen, &d.FragCount, &d.SHA256, &d.Payload, &ts); err != nil {
			return nil, err
		}
		d.Completed, _ = time.Parse(time.RFC3339Nano, ts)
		out = append(out, d)
	}
	return out, rows.Err()
}

func scanEvents(rows *sql.Rows) ([]EventRow, error) {
	var out []EventRow
	for rows.Next() {
		var e EventRow
		var ts string
		if err := rows.Scan(&e.Seq, &ts, &e.Category, &e.Src, &e.Dst, &e.Proto, &e.Ident, &e.Detail); err != nil {
			return nil, err
		}
		e.TS, _ = time.Parse(time.RFC3339Nano, ts)
		out = append(out, e)
	}
	return out, rows.Err()
}
