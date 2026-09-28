package storage

import (
	"tcpreplay/internal/reassembly"
)

// EventFilter narrows event queries; zero fields mean "all".
type EventFilter struct {
	Code      string
	Level     string
	Flow      string
	Direction string
	Limit     int
}

// ListEvents returns stored diagnostic events in emission order.
func (s *Store) ListEvents(requestID string, f EventFilter) ([]reassembly.Event, error) {
	q := `SELECT seq, record_id, ts, code, level, flow, generation, direction, msg,
	                raw_seq, abs_start, abs_end, next_contig, fin_pos, payload_hex, preview_total
	         FROM events WHERE request_id=?`
	args := []any{requestID}
	if f.Code != "" {
		q += ` AND code=?`
		args = append(args, f.Code)
	}
	if f.Level != "" {
		q += ` AND level=?`
		args = append(args, f.Level)
	}
	if f.Flow != "" {
		q += ` AND flow=?`
		args = append(args, f.Flow)
	}
	if f.Direction != "" {
		q += ` AND direction=?`
		args = append(args, f.Direction)
	}
	q += ` ORDER BY seq ASC`
	if f.Limit > 0 {
		q += ` LIMIT ?`
		args = append(args, f.Limit)
	}
	rows, err := s.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []reassembly.Event
	for rows.Next() {
		var e reassembly.Event
		var code, level string
		var rawSeq int64
		if err := rows.Scan(&e.Seq, &e.RecordID, &e.Timestamp, &code, &level, &e.Flow,
			&e.Generation, &e.Direction, &e.Msg, &rawSeq, &e.AbsStart, &e.AbsEnd,
			&e.NextContig, &e.FINPos, &e.PayloadPreview, &e.PreviewTotal); err != nil {
			return nil, err
		}
		e.RequestID = requestID
		e.Code = reassembly.EventCode(code)
		e.Level = reassembly.EventLevel(level)
		e.RawSeq = uint32(rawSeq)
		out = append(out, e)
	}
	return out, rows.Err()
}

// ListConflicts returns byte-level conflicts, optionally scoped to one
// flow/generation/direction (empty = any).
func (s *Store) ListConflicts(requestID, flow, direction string, generation int) ([]reassembly.Conflict, error) {
	q := `SELECT conflict_id, record_id, flow, generation, direction, byte_offset, raw_seq,
	                accepted, offered, accepted_by, policy, disposition, ts
	         FROM conflicts WHERE request_id=?`
	args := []any{requestID}
	if flow != "" {
		q += ` AND flow=?`
		args = append(args, flow)
	}
	if direction != "" {
		q += ` AND direction=?`
		args = append(args, direction)
	}
	if generation >= 0 {
		q += ` AND generation=?`
		args = append(args, generation)
	}
	q += ` ORDER BY flow, generation, direction, byte_offset`
	rows, err := s.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []reassembly.Conflict
	for rows.Next() {
		var c reassembly.Conflict
		var accepted, offered, rawSeq int64
		var policy string
		if err := rows.Scan(&c.ID, &c.RecordID, &c.Flow, &c.Generation, &c.Direction,
			&c.ByteOffset, &rawSeq, &accepted, &offered, &c.AcceptedBy,
			&policy, &c.Disposition, &c.Timestamp); err != nil {
			return nil, err
		}
		c.RequestID = requestID
		c.RawSeq = uint32(rawSeq)
		c.Accepted = byte(accepted)
		c.Offered = byte(offered)
		c.Policy = reassembly.OverlapPolicy(policy)
		out = append(out, c)
	}
	return out, rows.Err()
}

// ListPackets returns the archived metadata for the request's input packets.
func (s *Store) ListPackets(requestID string) ([]PacketMeta, error) {
	rows, err := s.db.Query(
		`SELECT idx, record_id, flow, direction, raw_seq, payload_len, flags, ts
		   FROM packets_archive WHERE request_id=? ORDER BY idx`, requestID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PacketMeta
	for rows.Next() {
		var p PacketMeta
		var rawSeq int64
		if err := rows.Scan(&p.Index, &p.RecordID, &p.Flow, &p.Direction, &rawSeq,
			&p.PayloadLen, &p.Flags, &p.Timestamp); err != nil {
			return nil, err
		}
		p.RawSeq = uint32(rawSeq)
		out = append(out, p)
	}
	return out, rows.Err()
}
