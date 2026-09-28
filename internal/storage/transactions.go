package storage

import (
	"context"
	"database/sql"
	"errors"
)

// TransactionRecord is the persisted decision for one (identity,xid,
// fingerprint) client request.
type TransactionRecord struct {
	ID          int64
	IdentityID  string
	XID         uint32
	Phase       string
	Fingerprint string
	InType      string
	OutType     string
	AssignedIP  string
	Reply       []byte
	LeaseEndsAt int64
	CreatedAt   int64
}

// LookupTransaction returns a previous decision for the exact dedup key, or
// nil when this is the first sighting.
func (s *Store) LookupTransaction(ctx context.Context, identityID string, xid uint32, fingerprint string) (*TransactionRecord, error) {
	const q = `SELECT id, identity_id, xid, phase, fingerprint, in_type, out_type,
	                  assigned_ip, reply, lease_ends_at, created_at
	           FROM transactions
	           WHERE identity_id=? AND xid=? AND fingerprint=? LIMIT 1`
	row := s.db.QueryRowContext(ctx, q, identityID, int64(xid), fingerprint)
	var t TransactionRecord
	err := row.Scan(&t.ID, &t.IdentityID, &t.XID, &t.Phase, &t.Fingerprint,
		&t.InType, &t.OutType, &t.AssignedIP, &t.Reply, &t.LeaseEndsAt, &t.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &t, nil
}

// SaveTransaction persists a new decision. The UNIQUE(identity,xid,fp)
// constraint turns a racing duplicate insert into a lookup-and-replay.
func (s *Store) SaveTransaction(ctx context.Context, t TransactionRecord) (inserted bool, prior *TransactionRecord, err error) {
	insErr := s.withImmediate(ctx, func(tx *sql.Tx) error {
		res, e := tx.ExecContext(ctx, `
			INSERT INTO transactions(identity_id, xid, phase, fingerprint, in_type,
			                          out_type, assigned_ip, reply, lease_ends_at, created_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			t.IdentityID, int64(t.XID), t.Phase, t.Fingerprint, t.InType,
			t.OutType, t.AssignedIP, t.Reply, t.LeaseEndsAt, t.CreatedAt)
		if e != nil {
			return e
		}
		id, _ := res.LastInsertId()
		t.ID = id
		inserted = true
		return nil
	})
	if insErr == nil {
		return true, nil, nil
	}
	if !isUniqueConstraint(insErr) {
		return false, nil, insErr
	}
	// Racer inserted first: fetch their row for replay.
	got, lerr := s.LookupTransaction(ctx, identityOf(t), t.XID, t.Fingerprint)
	if lerr != nil {
		return false, nil, lerr
	}
	return false, got, nil
}

func identityOf(t TransactionRecord) string { return t.IdentityID }

// InsertEvent appends a structured decision event.
func (s *Store) InsertEvent(ctx context.Context, e Event) (int64, error) {
	res, err := s.db.ExecContext(ctx, `
		INSERT INTO events(run_id, ts_nanos, xid, identity_id, mac, remote_addr,
		                   in_type, out_type, action, result, reason, assigned_ip, detail)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		e.RunID, e.TsNanos, int64(e.XID), e.IdentityID, e.MAC, e.RemoteAddr,
		e.InType, e.OutType, e.Action, e.Result, e.Reason, e.AssignedIP, e.Detail)
	if err != nil {
		return 0, err
	}
	id, _ := res.LastInsertId()
	return id, nil
}

// RecentEvents returns up to limit events (newest last when asc=true).
func (s *Store) RecentEvents(ctx context.Context, runID string, limit int, asc bool) ([]Event, error) {
	if limit <= 0 || limit > 5000 {
		limit = 100
	}
	dir := "DESC"
	if asc {
		dir = "ASC"
	}
	q := `SELECT id, run_id, ts_nanos, xid, identity_id, mac, remote_addr,
	             in_type, out_type, action, result, reason, assigned_ip, detail
	      FROM events`
	args := []any{}
	if runID != "" {
		q += ` WHERE run_id=?`
		args = append(args, runID)
	}
	q += ` ORDER BY id ` + dir + ` LIMIT ?`
	args = append(args, limit)
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Event
	for rows.Next() {
		var e Event
		if err := rows.Scan(&e.ID, &e.RunID, &e.TsNanos, &e.XID, &e.IdentityID, &e.MAC,
			&e.RemoteAddr, &e.InType, &e.OutType, &e.Action, &e.Result,
			&e.Reason, &e.AssignedIP, &e.Detail); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}
