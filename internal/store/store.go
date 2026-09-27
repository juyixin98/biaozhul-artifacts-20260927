// Package store is the SQLite-backed state store: connections and handshake
// generations, the original packet ledger, delivered stream chunks, gap and
// conflict evidence, and diagnostic records.
//
// The store is evidence-oriented. Nothing is deleted when gaps fill or
// conflicts resolve; rows gain a status instead, so a reviewer can always
// reconstruct what was missing and when it arrived.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	_ "modernc.org/sqlite"

	"tcpreasm/internal/diag"
)

// ErrNotFound is returned by lookup helpers.
var ErrNotFound = errors.New("not found")

// Store wraps a database handle.
type Store struct {
	db *sql.DB
}

// schema is applied on every Open inside one migration step.
const schema = `
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
  flow_key    TEXT PRIMARY KEY,
  endpoint_a  TEXT NOT NULL,
  endpoint_b  TEXT NOT NULL,
  client_ep   TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  updated_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  flow_key        TEXT NOT NULL REFERENCES connections(flow_key),
  gen_index       INTEGER NOT NULL,
  inferred        INTEGER NOT NULL DEFAULT 0,
  c2s_state       TEXT NOT NULL,
  s2c_state       TEXT NOT NULL,
  c2s_isn         INTEGER,
  s2c_isn         INTEGER,
  c2s_rcv_nxt_abs INTEGER,
  s2c_rcv_nxt_abs INTEGER,
  c2s_delivered   INTEGER NOT NULL DEFAULT 0,
  s2c_delivered   INTEGER NOT NULL DEFAULT 0,
  c2s_fin_end_abs INTEGER,
  s2c_fin_end_abs INTEGER,
  created_seq     INTEGER NOT NULL,
  updated_seq     INTEGER NOT NULL,
  UNIQUE(flow_key, gen_index)
);

CREATE TABLE IF NOT EXISTS packets (
  record_id   TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'capture',
  obs_order   INTEGER NOT NULL,
  flow_key    TEXT NOT NULL DEFAULT '',
  gen_index   INTEGER,
  direction   TEXT NOT NULL DEFAULT '',
  syn INTEGER NOT NULL DEFAULT 0, fin INTEGER NOT NULL DEFAULT 0,
  rst INTEGER NOT NULL DEFAULT 0, ack_flag INTEGER NOT NULL DEFAULT 0,
  seq32       INTEGER NOT NULL DEFAULT 0,
  ack32       INTEGER NOT NULL DEFAULT 0,
  seg_seq_abs INTEGER NOT NULL DEFAULT 0,
  seg_end_abs INTEGER NOT NULL DEFAULT 0,
  payload     BLOB,
  payload_len INTEGER NOT NULL DEFAULT 0,
  decision    TEXT NOT NULL DEFAULT '',
  category    TEXT NOT NULL DEFAULT '',
  ingest_seq  INTEGER NOT NULL,
  PRIMARY KEY (source, record_id)
);
CREATE INDEX IF NOT EXISTS idx_packets_flow ON packets(flow_key, gen_index, direction, seg_seq_abs);
CREATE INDEX IF NOT EXISTS idx_packets_ingest ON packets(ingest_seq);

CREATE TABLE IF NOT EXISTS stream_chunks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  flow_key    TEXT NOT NULL,
  gen_index   INTEGER NOT NULL,
  direction   TEXT NOT NULL,
  stream_off  INTEGER NOT NULL,
  data        BLOB NOT NULL,
  packet_record_id TEXT NOT NULL DEFAULT '',
  delivered_ingest_seq INTEGER NOT NULL,
  UNIQUE(flow_key, gen_index, direction, stream_off, id)
);
CREATE INDEX IF NOT EXISTS idx_chunks_order
  ON stream_chunks(flow_key, gen_index, direction, stream_off);

CREATE TABLE IF NOT EXISTS gaps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  flow_key    TEXT NOT NULL,
  gen_index   INTEGER NOT NULL,
  direction   TEXT NOT NULL,
  start_off   INTEGER NOT NULL,
  end_off     INTEGER NOT NULL,
  status      TEXT NOT NULL,
  opened_ingest_seq INTEGER NOT NULL,
  filled_record_id TEXT NOT NULL DEFAULT '',
  filled_ingest_seq INTEGER,
  UNIQUE(flow_key, gen_index, direction, start_off, end_off)
);

CREATE TABLE IF NOT EXISTS conflicts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  flow_key    TEXT NOT NULL,
  gen_index   INTEGER NOT NULL,
  direction   TEXT NOT NULL,
  start_abs   INTEGER NOT NULL,
  end_abs     INTEGER NOT NULL,
  start_off   INTEGER NOT NULL,
  end_off     INTEGER NOT NULL,
  incumbent_record_id TEXT NOT NULL DEFAULT '',
  newcomer_record_id  TEXT NOT NULL DEFAULT '',
  incumbent_sha TEXT NOT NULL DEFAULT '',
  newcomer_sha  TEXT NOT NULL DEFAULT '',
  policy      TEXT NOT NULL,
  winner      TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',
  ingest_seq  INTEGER NOT NULL,
  UNIQUE(flow_key, gen_index, direction, start_abs, end_abs, newcomer_record_id)
);

CREATE TABLE IF NOT EXISTS diagnostics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  TEXT NOT NULL DEFAULT '',
  record_id   TEXT NOT NULL DEFAULT '',
  flow_key    TEXT NOT NULL DEFAULT '',
  direction   TEXT NOT NULL DEFAULT '',
  decision    TEXT NOT NULL,
  category    TEXT NOT NULL,
  reason      TEXT NOT NULL DEFAULT '',
  gen_index   INTEGER,
  seg_seq_abs INTEGER NOT NULL DEFAULT 0,
  seg_end_abs INTEGER NOT NULL DEFAULT 0,
  payload_len INTEGER NOT NULL DEFAULT 0,
  payload_sha TEXT NOT NULL DEFAULT '',
  payload_preview TEXT NOT NULL DEFAULT '',
  state_json  TEXT NOT NULL DEFAULT '{}',
  conflict_id INTEGER,
  src TEXT NOT NULL DEFAULT '',
  dst TEXT NOT NULL DEFAULT '',
  ingest_seq  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diag_flow ON diagnostics(flow_key, gen_index);
CREATE INDEX IF NOT EXISTS idx_diag_req ON diagnostics(request_id);
CREATE INDEX IF NOT EXISTS idx_diag_cat ON diagnostics(decision, category);
`

// Open creates/opens the database and applies the schema. The directory of
// dsn must exist for non ":memory:" files.
func Open(ctx context.Context, dsn string) (*Store, error) {
	if dsn != ":memory:" && !strings.Contains(dsn, "?") {
		// Enable WAL and busy timeout via PRAGMAs below; keep DSN simple.
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// One connection avoids SQLITE_BUSY under our single-process load and
	// makes in-memory DSNs share one schema.
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	s := &Store{db: db}
	if _, err := db.ExecContext(ctx, "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON; PRAGMA synchronous=NORMAL;"); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.ExecContext(ctx, schema); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the handle.
func (s *Store) Close() error { return s.db.Close() }

// DB exposes the handle for tests/read-only introspection.
func (s *Store) DB() *sql.DB { return s.db }

// NextIngestSeq returns the monotonically increasing ingestion sequence
// used to tie diagnostics, gaps and chunks to one observation step.
func (s *Store) NextIngestSeq(ctx context.Context) (int64, error) {
	var n int64
	row := s.db.QueryRowContext(ctx,
		`SELECT COALESCE(MAX(ingest_seq),0)+1 FROM (
			SELECT ingest_seq FROM packets
			UNION ALL SELECT delivered_ingest_seq FROM stream_chunks
			UNION ALL SELECT opened_ingest_seq FROM gaps
			UNION ALL SELECT ingest_seq FROM conflicts
			UNION ALL SELECT ingest_seq FROM diagnostics)`)
	if err := row.Scan(&n); err != nil {
		return 0, err
	}
	return n, nil
}

// ---- connections / generations -------------------------------------------------

// UpsertConnection creates or updates a connection row.
func (s *Store) UpsertConnection(ctx context.Context, c ConnectionRow) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO connections(flow_key, endpoint_a, endpoint_b, client_ep, state, created_seq, updated_seq)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(flow_key) DO UPDATE SET state=excluded.state, updated_seq=excluded.updated_seq,
  client_ep=CASE WHEN connections.client_ep='' THEN excluded.client_ep ELSE connections.client_ep END`,
		c.FlowKey, c.EndpointA, c.EndpointB, c.ClientEP, c.State, c.CreatedSeq, c.UpdatedSeq)
	return err
}

// ConnectionRow mirrors the connections table.
type ConnectionRow struct {
	FlowKey, EndpointA, EndpointB, ClientEP, State string
	CreatedSeq, UpdatedSeq                         int64
}

// GenerationRow mirrors the generations table. Nullable ISN/offsets use
// pointers: NULL handshake value is meaningful (unknown ISN).
type GenerationRow struct {
	FlowKey                      string
	GenIndex                     int
	Inferred                     bool
	C2SState, S2CState           string
	C2SISN, S2CISN               *uint32
	C2SRcvNxtAbs, S2CRcvNxtAbs   *uint64
	C2SDelivered, S2CDelivered   uint64
	C2SFineEndAbs, S2CFineEndAbs *uint64
	CreatedSeq, UpdatedSeq       int64
}

// InsertGeneration adds a new generation row.
func (s *Store) InsertGeneration(ctx context.Context, g GenerationRow) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO generations(flow_key, gen_index, inferred, c2s_state, s2c_state,
  c2s_isn, s2c_isn, c2s_rcv_nxt_abs, s2c_rcv_nxt_abs, c2s_delivered, s2c_delivered,
  c2s_fin_end_abs, s2c_fin_end_abs, created_seq, updated_seq)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		g.FlowKey, g.GenIndex, btoi(g.Inferred), g.C2SState, g.S2CState,
		u32p(g.C2SISN), u32p(g.S2CISN), u64p(g.C2SRcvNxtAbs), u64p(g.S2CRcvNxtAbs),
		g.C2SDelivered, g.S2CDelivered, u64p(g.C2SFineEndAbs), u64p(g.S2CFineEndAbs),
		g.CreatedSeq, g.UpdatedSeq)
	return err
}

// UpsertGeneration inserts a generation row or updates it by (flow,index).
func (s *Store) UpsertGeneration(ctx context.Context, g GenerationRow) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO generations(flow_key, gen_index, inferred, c2s_state, s2c_state,
  c2s_isn, s2c_isn, c2s_rcv_nxt_abs, s2c_rcv_nxt_abs,
  c2s_delivered, s2c_delivered, c2s_fin_end_abs, s2c_fin_end_abs, created_seq, updated_seq)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(flow_key, gen_index) DO UPDATE SET
  inferred=excluded.inferred, c2s_state=excluded.c2s_state, s2c_state=excluded.s2c_state,
  c2s_isn=excluded.c2s_isn, s2c_isn=excluded.s2c_isn,
  c2s_rcv_nxt_abs=excluded.c2s_rcv_nxt_abs, s2c_rcv_nxt_abs=excluded.s2c_rcv_nxt_abs,
  c2s_delivered=excluded.c2s_delivered, s2c_delivered=excluded.s2c_delivered,
  c2s_fin_end_abs=excluded.c2s_fin_end_abs, s2c_fin_end_abs=excluded.s2c_fin_end_abs,
  updated_seq=excluded.updated_seq`,
		g.FlowKey, g.GenIndex, btoi(g.Inferred), g.C2SState, g.S2CState,
		u32p(g.C2SISN), u32p(g.S2CISN), u64p(g.C2SRcvNxtAbs), u64p(g.S2CRcvNxtAbs),
		g.C2SDelivered, g.S2CDelivered, u64p(g.C2SFineEndAbs), u64p(g.S2CFineEndAbs),
		g.CreatedSeq, g.UpdatedSeq)
	return err
}

// SaveGeneration persists current per-generation state.
func (s *Store) SaveGeneration(ctx context.Context, g GenerationRow) error {
	res, err := s.db.ExecContext(ctx, `
UPDATE generations SET inferred=?, c2s_state=?, s2c_state=?,
  c2s_isn=?, s2c_isn=?, c2s_rcv_nxt_abs=?, s2c_rcv_nxt_abs=?,
  c2s_delivered=?, s2c_delivered=?, c2s_fin_end_abs=?, s2c_fin_end_abs=?, updated_seq=?
WHERE flow_key=? AND gen_index=?`,
		btoi(g.Inferred), g.C2SState, g.S2CState,
		u32p(g.C2SISN), u32p(g.S2CISN), u64p(g.C2SRcvNxtAbs), u64p(g.S2CRcvNxtAbs),
		g.C2SDelivered, g.S2CDelivered, u64p(g.C2SFineEndAbs), u64p(g.S2CFineEndAbs),
		g.UpdatedSeq, g.FlowKey, g.GenIndex)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return fmt.Errorf("SaveGeneration: affected %d rows for %s gen %d", n, g.FlowKey, g.GenIndex)
	}
	return nil
}

// GenerationRowOut is a read model for generations.
type GenerationRowOut struct {
	GenIndex     int     `json:"gen_index"`
	Inferred     bool    `json:"inferred"`
	C2SState     string  `json:"c2s_state"`
	S2CState     string  `json:"s2c_state"`
	C2SISN       *uint32 `json:"c2s_isn,omitempty"`
	S2CISN       *uint32 `json:"s2c_isn,omitempty"`
	C2SDelivered uint64  `json:"c2s_delivered"`
	S2CDelivered uint64  `json:"s2c_delivered"`
	CreatedSeq   int64   `json:"created_seq"`
	UpdatedSeq   int64   `json:"updated_seq"`
}

// ConnectionOut is a read model for connections.
type ConnectionOut struct {
	FlowKey   string             `json:"flow_key"`
	State     string             `json:"state"`
	EndpointA string             `json:"endpoint_a"`
	EndpointB string             `json:"endpoint_b"`
	ClientEP  string             `json:"client_ep"`
	Gens      []GenerationRowOut `json:"generations"`
}

// GetConnection reads one connection with all generations, oldest first.
func (s *Store) GetConnection(ctx context.Context, flowKey string) (ConnectionOut, error) {
	var co ConnectionOut
	var ca, cb, cep string
	err := s.db.QueryRowContext(ctx,
		`SELECT flow_key, endpoint_a, endpoint_b, client_ep, state FROM connections WHERE flow_key=?`,
		flowKey).Scan(&co.FlowKey, &ca, &cb, &cep, &co.State)
	if errors.Is(err, sql.ErrNoRows) {
		return ConnectionOut{}, ErrNotFound
	}
	if err != nil {
		return ConnectionOut{}, err
	}
	co.EndpointA, co.EndpointB, co.ClientEP = ca, cb, cep
	rows, err := s.db.QueryContext(ctx, `
SELECT gen_index, inferred, c2s_state, s2c_state, c2s_isn, s2c_isn,
       c2s_delivered, s2c_delivered, created_seq, updated_seq
FROM generations WHERE flow_key=? ORDER BY gen_index`, flowKey)
	if err != nil {
		return ConnectionOut{}, err
	}
	type genRow struct {
		g          GenerationRowOut
		isnA, isnB sql.NullInt64
		inferred   int
	}
	var fetched []genRow
	for rows.Next() {
		var rr genRow
		if err := rows.Scan(&rr.g.GenIndex, &rr.inferred, &rr.g.C2SState, &rr.g.S2CState,
			&rr.isnA, &rr.isnB, &rr.g.C2SDelivered, &rr.g.S2CDelivered,
			&rr.g.CreatedSeq, &rr.g.UpdatedSeq); err != nil {
			_ = rows.Close()
			return ConnectionOut{}, err
		}
		fetched = append(fetched, rr)
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return ConnectionOut{}, err
	}
	if err := rows.Close(); err != nil {
		return ConnectionOut{}, err
	}
	for _, rr := range fetched {
		g := rr.g
		g.Inferred = rr.inferred != 0
		if rr.isnA.Valid {
			v := uint32(rr.isnA.Int64)
			g.C2SISN = &v
		}
		if rr.isnB.Valid {
			v := uint32(rr.isnB.Int64)
			g.S2CISN = &v
		}
		co.Gens = append(co.Gens, g)
	}
	return co, nil
}

// ListConnections returns all flow keys, newest activity first.
func (s *Store) ListConnections(ctx context.Context) ([]ConnectionOut, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT flow_key FROM connections ORDER BY updated_seq DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var keys []string
	for rows.Next() {
		var k string
		if err := rows.Scan(&k); err != nil {
			return nil, err
		}
		keys = append(keys, k)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]ConnectionOut, 0, len(keys))
	for _, k := range keys {
		co, err := s.GetConnection(ctx, k)
		if err != nil {
			return nil, err
		}
		out = append(out, co)
	}
	return out, nil
}

// ---- packet ledger --------------------------------------------------------------

// PacketRow is one stored packet record.
type PacketRow struct {
	RecordID, Source       string
	ObsOrder               int64
	FlowKey                string
	GenIndex               *int
	Direction              string
	SYN, FIN, RST, AckFlag bool
	Seq32, Ack32           uint32
	SegSeqAbs, SegEndAbs   uint64
	Payload                []byte
	Decision, Category     string
	IngestSeq              int64
}

// InsertPacket stores the raw packet plus its routing decision.
func (s *Store) InsertPacket(ctx context.Context, p PacketRow) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO packets(record_id, source, obs_order, flow_key, gen_index, direction,
  syn, fin, rst, ack_flag, seq32, ack32, seg_seq_abs, seg_end_abs,
  payload, payload_len, decision, category, ingest_seq)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(source, record_id) DO NOTHING`,
		p.RecordID, p.Source, p.ObsOrder, p.FlowKey, p.GenIndex, p.Direction,
		btoi(p.SYN), btoi(p.FIN), btoi(p.RST), btoi(p.AckFlag),
		int64(p.Seq32), int64(p.Ack32), int64(p.SegSeqAbs), int64(p.SegEndAbs),
		p.Payload, len(p.Payload), p.Decision, p.Category, p.IngestSeq)
	return err
}

// PacketExists reports whether a (source,record_id) packet was ingested.
func (s *Store) PacketExists(ctx context.Context, source, recordID string) (bool, error) {
	var n int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM packets WHERE source=? AND record_id=?`, source, recordID).Scan(&n)
	return n > 0, err
}

// ---- stream chunks / gaps / conflicts ------------------------------------------

// InsertChunk appends one delivered byte range (the row carries provenance).
func (s *Store) InsertChunk(ctx context.Context, c ChunkRow) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO stream_chunks(flow_key, gen_index, direction, stream_off, data, packet_record_id, delivered_ingest_seq)
VALUES(?,?,?,?,?,?,?)`,
		c.FlowKey, c.GenIndex, c.Direction, c.StreamOff, c.Data, c.RecordID, c.IngestSeq)
	return err
}

// ChunkRow mirrors stream_chunks.
type ChunkRow struct {
	FlowKey   string
	GenIndex  int
	Direction string
	StreamOff uint64
	Data      []byte
	RecordID  string
	IngestSeq int64
}

// StreamByteRange returns the contiguous delivered bytes for a direction in
// offset interval [start,end). Rows are stored as non-overlapping delivery
// chunks; any missing interior byte makes the function return what exists up
// to the hole plus contig=false so callers never mistake a prefix for more.
func (s *Store) StreamByteRange(ctx context.Context, flowKey string, gen int, direction string, start, end uint64, limit int) (data []byte, contig bool, totalDelivered uint64, err error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT stream_off, data FROM stream_chunks
WHERE flow_key=? AND gen_index=? AND direction=? AND stream_off < ?
ORDER BY stream_off`, flowKey, gen, direction, end)
	if err != nil {
		return nil, false, 0, err
	}
	type row struct {
		off uint64
		d   []byte
	}
	var fetched []row
	for rows.Next() {
		var rr row
		if err := rows.Scan(&rr.off, &rr.d); err != nil {
			_ = rows.Close()
			return nil, false, 0, err
		}
		fetched = append(fetched, rr)
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return nil, false, 0, err
	}
	if err := rows.Close(); err != nil {
		return nil, false, 0, err
	}
	next := start
	var buf []byte
	truncated := false
	for _, rr := range fetched {
		off, d := rr.off, rr.d
		if uint64(off)+uint64(len(d)) <= start {
			continue // fully before requested window
		}
		if off > next {
			break // hole
		}
		begin := uint64(0)
		if off < start {
			begin = start - off
		}
		tail := d[begin:]
		if uint64(off)+uint64(len(d)) > end {
			tail = tail[:end-off-begin]
		}
		if limit >= 0 && len(buf)+len(tail) > limit {
			tail = tail[:max(0, limit-len(buf))]
			truncated = true
		}
		buf = append(buf, tail...)
		next = off + uint64(len(d))
		if next >= end || truncated {
			break
		}
	}
	var maxEnd sql.NullInt64
	if err := s.db.QueryRowContext(ctx, `
SELECT MAX(stream_off + length(data)) FROM stream_chunks
WHERE flow_key=? AND gen_index=? AND direction=?`, flowKey, gen, direction).
		Scan(&maxEnd); err != nil {
		return nil, false, 0, err
	}
	if maxEnd.Valid {
		totalDelivered = uint64(maxEnd.Int64)
	}
	contig = uint64(len(buf)) == end-start || truncated
	return buf, contig, totalDelivered, nil
}

// GapRow mirrors gaps.
type GapRow struct {
	FlowKey        string `json:"flow_key"`
	GenIndex       int    `json:"gen_index"`
	Direction      string `json:"direction"`
	StartOff       uint64 `json:"start_off"`
	EndOff         uint64 `json:"end_off"`
	Status         string `json:"status"`
	FilledRecordID string `json:"filled_record_id,omitempty"`
}

// OpenGap inserts a gap evidence row if absent.
func (s *Store) OpenGap(ctx context.Context, g GapRow, ingestSeq int64) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO gaps(flow_key, gen_index, direction, start_off, end_off, status, opened_ingest_seq)
VALUES(?,?,?,?,?,'open',?)
ON CONFLICT(flow_key, gen_index, direction, start_off, end_off) DO NOTHING`,
		g.FlowKey, g.GenIndex, g.Direction, g.StartOff, g.EndOff, ingestSeq)
	return err
}

// FillGap marks a gap filled, naming the packet that provided the bytes.
func (s *Store) FillGap(ctx context.Context, flowKey string, gen int, dir string, startOff, endOff uint64, recordID string, ingestSeq int64) error {
	_, err := s.db.ExecContext(ctx, `
UPDATE gaps SET status='filled', filled_record_id=?, filled_ingest_seq=?
WHERE flow_key=? AND gen_index=? AND direction=? AND start_off=? AND end_off=?`,
		recordID, ingestSeq, flowKey, gen, dir, startOff, endOff)
	return err
}

// OpenGaps returns [start,end) intervals currently open for a direction.
func (s *Store) OpenGaps(ctx context.Context, flowKey string, gen int, dir string) ([][2]uint64, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT start_off, end_off FROM gaps
WHERE flow_key=? AND gen_index=? AND direction=? AND status='open'
ORDER BY start_off`, flowKey, gen, dir)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out [][2]uint64
	for rows.Next() {
		var g [2]uint64
		if err := rows.Scan(&g[0], &g[1]); err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// ReconcileOpenGap records the single hole currently ahead of the delivered
// prefix: any overlapping open gap rows are replaced by exactly [s,e).
// Non-overlapping rows (e.g. a separate poisoned-held hole handled by the
// conflict ledger) are untouched.
func (s *Store) ReconcileOpenGap(ctx context.Context, flowKey string, gen int, dir string, sOff, eOff uint64, ingestSeq int64) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.ExecContext(ctx, `
DELETE FROM gaps
WHERE flow_key=? AND gen_index=? AND direction=? AND status='open'
  AND start_off < ? AND end_off > ?`,
		flowKey, gen, dir, eOff, sOff); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
INSERT INTO gaps(flow_key, gen_index, direction, start_off, end_off, status, opened_ingest_seq)
VALUES(?,?,?,?,?,'open',?)
ON CONFLICT(flow_key, gen_index, direction, start_off, end_off) DO NOTHING`,
		flowKey, gen, dir, sOff, eOff, ingestSeq); err != nil {
		return err
	}
	return tx.Commit()
}

// FillGapsUpTo marks every open gap ending at or before frontier as filled,
// and shrinks gaps straddling the frontier to start at frontier.
func (s *Store) FillGapsUpTo(ctx context.Context, flowKey string, gen int, dir string, frontier uint64, recordID string, ingestSeq int64) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.ExecContext(ctx, `
UPDATE gaps SET status='filled', filled_record_id=?, filled_ingest_seq=?
WHERE flow_key=? AND gen_index=? AND direction=? AND status='open' AND end_off <= ?`,
		recordID, ingestSeq, flowKey, gen, dir, frontier); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
UPDATE gaps SET start_off=?
WHERE flow_key=? AND gen_index=? AND direction=? AND status='open' AND start_off < ? AND end_off > ?`,
		frontier, flowKey, gen, dir, frontier, frontier); err != nil {
		return err
	}
	return tx.Commit()
}

// ListGaps returns gap rows optionally filtered by status ("" = all).
func (s *Store) ListGaps(ctx context.Context, flowKey string, gen int, dir, status string) ([]GapRow, error) {
	q := `SELECT flow_key, gen_index, direction, start_off, end_off, status, COALESCE(filled_record_id,'')
FROM gaps WHERE flow_key=? AND gen_index=?`
	args := []any{flowKey, gen}
	if dir != "" {
		q += ` AND direction=?`
		args = append(args, dir)
	}
	if status != "" {
		q += ` AND status=?`
		args = append(args, status)
	}
	q += ` ORDER BY direction, start_off`
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []GapRow
	for rows.Next() {
		var g GapRow
		if err := rows.Scan(&g.FlowKey, &g.GenIndex, &g.Direction, &g.StartOff, &g.EndOff, &g.Status, &g.FilledRecordID); err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// ConflictRow mirrors conflicts.
type ConflictRow struct {
	FlowKey           string `json:"flow_key"`
	Direction         string `json:"direction"`
	GenIndex          int    `json:"gen_index"`
	StartAbs          uint64 `json:"start_abs"`
	EndAbs            uint64 `json:"end_abs"`
	StartOff          uint64 `json:"start_off"`
	EndOff            uint64 `json:"end_off"`
	IncumbentRecordID string `json:"incumbent_record_id"`
	NewcomerRecordID  string `json:"newcomer_record_id"`
	IncumbentSHA      string `json:"incumbent_sha"`
	NewcomerSHA       string `json:"newcomer_sha"`
	Policy            string `json:"policy"`
	Winner            string `json:"winner"`
	Status            string `json:"status"`
	IngestSeq         int64  `json:"ingest_seq"`
}

// InsertConflict records one contradictory overlap.
func (s *Store) InsertConflict(ctx context.Context, c ConflictRow) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO conflicts(flow_key, gen_index, direction, start_abs, end_abs, start_off, end_off,
  incumbent_record_id, newcomer_record_id, incumbent_sha, newcomer_sha, policy, winner, status, ingest_seq)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(flow_key, gen_index, direction, start_abs, end_abs, newcomer_record_id) DO NOTHING`,
		c.FlowKey, c.GenIndex, c.Direction, u64(c.StartAbs), u64(c.EndAbs), c.StartOff, c.EndOff,
		c.IncumbentRecordID, c.NewcomerRecordID, c.IncumbentSHA, c.NewcomerSHA,
		c.Policy, c.Winner, c.Status, c.IngestSeq)
	return err
}

// ConflictOut is the read model including ids.
type ConflictOut struct {
	ID int64 `json:"id"`
	ConflictRow
}

// ListConflicts returns conflicts for a generation, oldest first.
func (s *Store) ListConflicts(ctx context.Context, flowKey string, gen int, dir string) ([]ConflictOut, error) {
	q := `SELECT id, flow_key, gen_index, direction, start_abs, end_abs, start_off, end_off,
  COALESCE(incumbent_record_id,''), COALESCE(newcomer_record_id,''),
  COALESCE(incumbent_sha,''), COALESCE(newcomer_sha,''), policy, winner, status, ingest_seq
FROM conflicts WHERE flow_key=? AND gen_index=?`
	args := []any{flowKey, gen}
	if dir != "" {
		q += ` AND direction=?`
		args = append(args, dir)
	}
	q += ` ORDER BY id`
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ConflictOut
	for rows.Next() {
		var c ConflictOut
		if err := rows.Scan(&c.ID, &c.FlowKey, &c.GenIndex, &c.Direction,
			&c.StartAbs, &c.EndAbs, &c.StartOff, &c.EndOff,
			&c.IncumbentRecordID, &c.NewcomerRecordID,
			&c.IncumbentSHA, &c.NewcomerSHA, &c.Policy, &c.Winner, &c.Status, &c.IngestSeq); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// ---- diagnostics ----------------------------------------------------------------

// DiagOut is the read model of a diagnostic row.
type DiagOut struct {
	ID         int64  `json:"id"`
	RequestID  string `json:"request_id"`
	RecordID   string `json:"record_id"`
	FlowKey    string `json:"flow_key"`
	Direction  string `json:"direction"`
	Decision   string `json:"decision"`
	Category   string `json:"category"`
	Reason     string `json:"reason"`
	GenIndex   *int   `json:"gen_index,omitempty"`
	SegSeqAbs  uint64 `json:"seg_seq_abs"`
	SegEndAbs  uint64 `json:"seg_end_abs"`
	PayloadLen int    `json:"payload_len"`
	PayloadSHA string `json:"payload_sha"`
	Preview    string `json:"payload_preview"`
	StateJSON  string `json:"state_json"`
	Src        string `json:"src"`
	Dst        string `json:"dst"`
	IngestSeq  int64  `json:"ingest_seq"`
}

// InsertDiagnostic implements diag.Recorder.
func (s *Store) InsertDiagnostic(r diag.Record) error {
	return s.InsertDiagnosticRow(context.Background(), r, 0)
}

// InsertDiagnosticRow stores one record with explicit ingest sequence.
func (s *Store) InsertDiagnosticRow(ctx context.Context, r diag.Record, ingestSeq int64) error {
	gen := r.State.Generation
	var genP *int
	if gen != 0 || r.State.InferredGen {
		g := gen
		genP = &g
	}
	plen, psha, prev := 0, "", ""
	if r.Payload != nil {
		plen, psha, prev = r.Payload.Length, r.Payload.SHA256, r.Payload.Preview
	}
	stateJSON := stateToJSON(r.State)
	var conflictID *int64
	_, err := s.db.ExecContext(ctx, `
INSERT INTO diagnostics(request_id, record_id, flow_key, direction, decision, category, reason,
  gen_index, seg_seq_abs, seg_end_abs, payload_len, payload_sha, payload_preview,
  state_json, conflict_id, src, dst, ingest_seq)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		r.RequestID, r.RecordID, r.FlowKey, stringOr(r.Direction), r.Decision, r.Category, r.Reason,
		genP, u64(r.SegSeqAbs), u64(r.SegEndAbs), plen, psha, prev, stateJSON, conflictID, r.Src, r.Dst, ingestSeq)
	return err
}

// ListDiagnostics filters diagnostics; empty arguments are wildcards.
func (s *Store) ListDiagnostics(ctx context.Context, requestID, flowKey string, gen int, category string, limit int) ([]DiagOut, error) {
	q := `SELECT id, request_id, record_id, flow_key, direction, decision, category, reason,
  gen_index, seg_seq_abs, seg_end_abs, payload_len, payload_sha, COALESCE(payload_preview,''),
  state_json, src, dst, ingest_seq
FROM diagnostics WHERE 1=1`
	var args []any
	if requestID != "" {
		q += ` AND request_id=?`
		args = append(args, requestID)
	}
	if flowKey != "" {
		q += ` AND flow_key=?`
		args = append(args, flowKey)
	}
	if gen >= 0 {
		q += ` AND gen_index=?`
		args = append(args, gen)
	}
	if category != "" {
		q += ` AND category=?`
		args = append(args, category)
	}
	q += ` ORDER BY id`
	if limit > 0 {
		q += fmt.Sprintf(` LIMIT %d`, limit)
	}
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DiagOut
	for rows.Next() {
		var d DiagOut
		if err := rows.Scan(&d.ID, &d.RequestID, &d.RecordID, &d.FlowKey, &d.Direction,
			&d.Decision, &d.Category, &d.Reason, &d.GenIndex, &d.SegSeqAbs, &d.SegEndAbs,
			&d.PayloadLen, &d.PayloadSHA, &d.Preview, &d.StateJSON, &d.Src, &d.Dst, &d.IngestSeq); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

func btoi(b bool) int {
	if b {
		return 1
	}
	return 0
}

func u32p(v *uint32) any {
	if v == nil {
		return nil
	}
	return int64(*v)
}

func u64p(v *uint64) any {
	if v == nil {
		return nil
	}
	// SQLite integers are signed 64-bit; absolute sequence numbers can
	// legitimately reach >= 2^63 after wrap lifting, so store the two's
	// complement bit pattern. No arithmetic is performed on these values
	// inside SQL.
	return int64(*v)
}

// u64 stores an absolute sequence number as its bit pattern in a signed
// 64-bit integer.
func u64(x uint64) int64 { return int64(x) }

func stringOr(v string) any {
	if v == "" {
		return ""
	}
	return v
}

func stateToJSON(st diag.SeqState) string {
	return fmt.Sprintf(`{"isn":%d,"rcv_nxt_abs":%d,"delivered_offset":%d,"buffered_bytes":%d,"fin_seen":%t,"fin_end_abs":%d,"closed":%t,"reset":%t,"generation":%d,"inferred_generation":%t}`,
		st.ISN, st.RcvNxtAbs, st.DeliveredOffset, st.BufferedBytes,
		st.FINSeen, st.FINEndAbs, st.Closed, st.Reset, st.Generation, st.InferredGen)
}
