package storage

import (
	"database/sql"
	"errors"
	"fmt"
	"time"

	"tcpreplay/internal/reassembly"
)

// ErrNotFound is returned when a request id has no stored analysis.
var ErrNotFound = errors.New("storage: request not found")

// RequestRecord is the header row of one ingest.
type RequestRecord struct {
	ID          string
	Source      string
	Policy      string
	Preview     bool
	PacketCount int
	CreatedAt   time.Time
}

// PacketMeta is the archived, payload-free metadata of one input packet.
type PacketMeta struct {
	Index      int    `json:"index"`
	RecordID   string `json:"record_id"`
	Flow       string `json:"flow"`
	Direction  string `json:"direction"`
	RawSeq     uint32 `json:"raw_seq"`
	PayloadLen int    `json:"payload_len"`
	Flags      string `json:"flags"`
	Timestamp  string `json:"timestamp"`
}

// SaveAnalysis writes the full result of one ingest in a single transaction.
func (s *Store) SaveAnalysis(rec RequestRecord, packets []PacketMeta, events []reassembly.Event,
	conflicts []reassembly.Conflict, views []reassembly.GenerationView) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	created := rec.CreatedAt
	if created.IsZero() {
		created = time.Now().UTC()
	}
	if _, err := tx.Exec(
		`INSERT INTO requests(id, source, policy, preview, packet_count, created_at)
		 VALUES(?,?,?,?,?,?)`,
		rec.ID, rec.Source, rec.Policy, rec.Preview, rec.PacketCount,
		created.Format(time.RFC3339Nano)); err != nil {
		return fmt.Errorf("insert request: %w", err)
	}

	for _, p := range packets {
		if _, err := tx.Exec(
			`INSERT INTO packets_archive(request_id, idx, record_id, flow, direction, raw_seq, payload_len, flags, ts)
			 VALUES(?,?,?,?,?,?,?,?,?)`,
			rec.ID, p.Index, p.RecordID, p.Flow, p.Direction, int64(p.RawSeq),
			p.PayloadLen, p.Flags, p.Timestamp); err != nil {
			return fmt.Errorf("insert packet %d: %w", p.Index, err)
		}
	}

	for _, e := range events {
		if _, err := tx.Exec(
			`INSERT INTO events(request_id, seq, record_id, ts, code, level, flow, generation,
			   direction, msg, raw_seq, abs_start, abs_end, next_contig, fin_pos, payload_hex, preview_total)
			 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			rec.ID, e.Seq, e.RecordID, e.Timestamp, string(e.Code), string(e.Level), e.Flow,
			e.Generation, e.Direction, e.Msg, int64(e.RawSeq), e.AbsStart, e.AbsEnd,
			e.NextContig, e.FINPos, e.PayloadPreview, e.PreviewTotal); err != nil {
			return fmt.Errorf("insert event %d: %w", e.Seq, err)
		}
	}

	for _, c := range conflicts {
		if _, err := tx.Exec(
			`INSERT INTO conflicts(request_id, conflict_id, record_id, flow, generation, direction,
			   byte_offset, raw_seq, accepted, offered, accepted_by, policy, disposition, ts)
			 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			rec.ID, c.ID, c.RecordID, c.Flow, c.Generation, c.Direction, c.ByteOffset,
			int64(c.RawSeq), int64(c.Accepted), int64(c.Offered), c.AcceptedBy,
			string(c.Policy), c.Disposition, c.Timestamp); err != nil {
			return fmt.Errorf("insert conflict %s: %w", c.ID, err)
		}
	}

	for _, v := range views {
		if err := insertView(tx, rec.ID, v); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// RequestHeader is a stored request without its child rows.
type RequestHeader struct {
	ID          string `json:"id"`
	Source      string `json:"source"`
	Policy      string `json:"policy"`
	Preview     bool   `json:"preview"`
	PacketCount int    `json:"packet_count"`
	CreatedAt   string `json:"created_at"`
}

// ListRequests returns stored requests, newest first.
func (s *Store) ListRequests(limit int) ([]RequestHeader, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.Query(
		`SELECT id, source, policy, preview, packet_count, created_at
		 FROM requests ORDER BY created_at DESC, id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RequestHeader
	for rows.Next() {
		var h RequestHeader
		var preview int
		if err := rows.Scan(&h.ID, &h.Source, &h.Policy, &preview, &h.PacketCount, &h.CreatedAt); err != nil {
			return nil, err
		}
		h.Preview = preview != 0
		out = append(out, h)
	}
	return out, rows.Err()
}

// GetRequest loads one header.
func (s *Store) GetRequest(id string) (RequestHeader, error) {
	var h RequestHeader
	var preview int
	err := s.db.QueryRow(
		`SELECT id, source, policy, preview, packet_count, created_at FROM requests WHERE id=?`,
		id).Scan(&h.ID, &h.Source, &h.Policy, &preview, &h.PacketCount, &h.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return RequestHeader{}, ErrNotFound
	}
	if err != nil {
		return RequestHeader{}, err
	}
	h.Preview = preview != 0
	return h, nil
}

// Exists reports whether id is already stored (used to reject duplicate POSTs).
func (s *Store) Exists(id string) (bool, error) {
	var n int
	if err := s.db.QueryRow(`SELECT COUNT(*) FROM requests WHERE id=?`, id).Scan(&n); err != nil {
		return false, err
	}
	return n > 0, nil
}
