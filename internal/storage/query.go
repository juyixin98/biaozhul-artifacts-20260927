package storage

import (
	"context"
	"time"
)

func unixNano(n int64) time.Time {
	if n == 0 {
		return time.Time{}
	}
	return time.Unix(0, n).UTC()
}

// JournalEntry is one row of the reply/diagnostic journal.
type JournalEntry struct {
	ID           int64
	CreatedAt    time.Time
	ClientKey    string
	XID          []byte
	RecvType     string
	Action       string
	ReplyType    string
	OfferedIP    string
	LeaseExpires time.Time
	Reason       string
}

// RecentJournal returns the newest journal rows for diagnostics.
func (s *Store) RecentJournal(ctx context.Context, limit int) ([]JournalEntry, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, created_at, client_key, xid, recv_type, action, reply_type,
		        offered_ip, lease_expires, reason
		   FROM replies ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []JournalEntry
	for rows.Next() {
		var (
			e                JournalEntry
			created, expires int64
			ip               []byte
		)
		if err := rows.Scan(&e.ID, &created, &e.ClientKey, &e.XID, &e.RecvType,
			&e.Action, &e.ReplyType, &ip, &expires, &e.Reason); err != nil {
			return nil, err
		}
		e.CreatedAt = unixNano(created)
		e.LeaseExpires = unixNano(expires)
		if len(ip) == 4 {
			e.OfferedIP = blobIP(ip).String()
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// EventEntry is one internal state event.
type EventEntry struct {
	ID        int64
	TS        time.Time
	ClientKey string
	XID       []byte
	Kind      string
	Detail    string
}

// RecentEvents returns the newest internal event rows.
func (s *Store) RecentEvents(ctx context.Context, limit int) ([]EventEntry, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, ts, client_key, xid, kind, detail
		   FROM events ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []EventEntry
	for rows.Next() {
		var (
			e  EventEntry
			ts int64
		)
		if err := rows.Scan(&e.ID, &ts, &e.ClientKey, &e.XID, &e.Kind, &e.Detail); err != nil {
			return nil, err
		}
		e.TS = unixNano(ts)
		out = append(out, e)
	}
	return out, rows.Err()
}
