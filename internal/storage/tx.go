package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/netip"

	"dhcp4lab/internal/dhcp4"
)

// errConflict signals a lost address race inside a transaction; the
// caller retries allocation from the next candidate.
var errConflict = errors.New("address race, try next candidate")

// beginImmediate opens a write transaction that holds the SQLite write
// lock for its whole duration, making decision+commit atomic even if the
// driver default were deferred.
func (s *Store) beginImmediate(ctx context.Context) (*rawTx, error) {
	conn, err := s.db.Conn(ctx)
	if err != nil {
		return nil, err
	}
	if _, err := conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		_ = conn.Close()
		return nil, err
	}
	return &rawTx{Conn: conn}, nil
}

// rawTx wraps a *sql.Conn so Commit/Rollback release the connection.
type rawTx struct {
	Conn *sql.Conn
	done bool
}

func (t *rawTx) Commit() error {
	if t.done {
		return nil
	}
	_, err := t.Conn.ExecContext(context.Background(), "COMMIT")
	t.done = true
	_ = t.Conn.Close()
	return err
}

func (t *rawTx) Rollback() error {
	if t.done {
		return nil
	}
	_, err := t.Conn.ExecContext(context.Background(), "ROLLBACK")
	t.done = true
	_ = t.Conn.Close()
	return err
}

// Sweep expires offers/leases whose deadline has passed. It is called by
// the background reaper and, more importantly, at the start of every
// mutating transaction ("sweep on touch") so correctness does not depend
// on the timer having fired.
func (s *Store) Sweep(ctx context.Context) (expiredOffers, expiredLeases int, err error) {
	tx, err := s.beginImmediate(ctx)
	if err != nil {
		return 0, 0, err
	}
	defer func() { _ = tx.Rollback() }()
	now := s.now().UnixNano()

	expiredOffers, expiredLeases, err = s.sweepLocked(ctx, tx, now)
	if err != nil {
		return 0, 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, 0, err
	}
	return expiredOffers, expiredLeases, nil
}

// sweepLocked performs expiry inside an already-open write transaction.
func (s *Store) sweepLocked(ctx context.Context, tx *rawTx, now int64) (int, int, error) {
	res, err := tx.Conn.ExecContext(ctx,
		`UPDATE offers SET superseded = 1 WHERE superseded = 0 AND expires_at <= ?`, now)
	if err != nil {
		return 0, 0, fmt.Errorf("expire offers: %w", err)
	}
	nOff, _ := res.RowsAffected()

	// Leases: live rows past their deadline become 'expired', which
	// releases the IP back to the pool (partial unique index drops them).
	res, err = tx.Conn.ExecContext(ctx,
		`UPDATE leases
		    SET state = 'expired', ended_at = ?, note = 'expired by sweep'
		  WHERE state = 'leased' AND expires_at <= ?`, now, now)
	if err != nil {
		return 0, 0, fmt.Errorf("expire leases: %w", err)
	}
	nLease, _ := res.RowsAffected()
	if nOff > 0 || nLease > 0 {
		if err := s.insertEventLocked(ctx, tx, "", nil, "sweep",
			fmt.Sprintf("expired offers=%d leases=%d at %d", nOff, nLease, now)); err != nil {
			return 0, 0, err
		}
	}
	return int(nOff), int(nLease), nil
}

// upsertClientLocked inserts the client row or returns its existing id.
func (s *Store) upsertClientLocked(ctx context.Context, tx *rawTx, id dhcp4.ClientIdentity, now int64) (int64, error) {
	key := id.Key()
	chaddr := fmt.Sprintf("%02x:%02x:%02x:%02x:%02x:%02x",
		id.CHAddr[0], id.CHAddr[1], id.CHAddr[2], id.CHAddr[3], id.CHAddr[4], id.CHAddr[5])
	if _, err := tx.Conn.ExecContext(ctx,
		`INSERT INTO clients(key, htype, chaddr, option_id, created_at, updated_at)
		 VALUES(?, ?, ?, ?, ?, ?)
		 ON CONFLICT(key) DO UPDATE SET updated_at = excluded.updated_at`,
		key, int(id.HType), chaddr, id.OptionID, now, now); err != nil {
		return 0, err
	}
	var cid int64
	if err := tx.Conn.QueryRowContext(ctx,
		`SELECT id FROM clients WHERE key = ?`, key).Scan(&cid); err != nil {
		return 0, err
	}
	return cid, nil
}

// findClientLocked returns the client row id for a key.
func findClientLocked(ctx context.Context, tx *rawTx, key string) (int64, bool, error) {
	var cid int64
	err := tx.Conn.QueryRowContext(ctx,
		`SELECT id FROM clients WHERE key = ?`, key).Scan(&cid)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return cid, true, nil
}

// ipBlob renders an IPv4 as 4 bytes for BLOB columns. An unset/zero
// address maps to four zero bytes (it never panics): such a value is
// only meaningful as "no address" and callers validate semantics first.
func ipBlob(a netip.Addr) []byte {
	if !a.IsValid() {
		return []byte{0, 0, 0, 0}
	}
	b := a.As4()
	return b[:]
}

func blobIP(b []byte) netip.Addr {
	if len(b) != 4 {
		return netip.Addr{}
	}
	return dhcp4.IPv4(b[0], b[1], b[2], b[3])
}

func (s *Store) insertEventLocked(ctx context.Context, tx *rawTx, clientKey string, xid []byte, kind, detail string) error {
	// id is auto-assigned from the rowid sequence. An earlier version
	// supplied it from an in-process atomic counter, which collided with
	// persisted ids after a real process restart and broke the first
	// post-restart transaction.
	_, err := tx.Conn.ExecContext(ctx,
		`INSERT INTO events(ts, client_key, xid, kind, detail)
		 VALUES(?, ?, ?, ?, ?)`,
		s.now().UnixNano(), clientKey, xid, kind, detail)
	return err
}

// insertReplyLocked writes the journal/dedup row. action must be one of
// the Action values; replyBytes may be nil for no-reply outcomes.
func (s *Store) insertReplyLocked(ctx context.Context, tx *rawTx, r journalRow) (int64, error) {
	res, err := tx.Conn.ExecContext(ctx,
		`INSERT INTO replies(created_at, client_key, xid, recv_type, action,
		                      reply_type, offered_ip, lease_expires,
		                      ref_kind, ref_id, reason, reply_bytes)
		 VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		r.createdAt, r.clientKey, r.xid, r.recvType, string(r.action),
		r.replyType, r.offeredIP, r.leaseExpires,
		r.refKind, r.refID, r.reason, r.replyBytes)
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

type journalRow struct {
	ID           int64
	createdAt    int64
	clientKey    string
	xid          []byte
	recvType     string
	action       Action
	replyType    string
	offeredIP    []byte
	leaseExpires int64
	// refKind/refID pinpoint the offer/lease row this reply granted.
	refKind    string
	refID      int64
	reason     string
	replyBytes []byte
}

// findDuplicateReplyLocked looks for an existing dedup row for
// (identity, xid, received type).
func findDuplicateReplyLocked(ctx context.Context, tx *rawTx, clientKey string, xid []byte, recvType string) (*journalRow, error) {
	row := tx.Conn.QueryRowContext(ctx,
		`SELECT id, created_at, action, reply_type, offered_ip, lease_expires,
		        ref_kind, ref_id, reason, reply_bytes
		   FROM replies
		  WHERE client_key = ? AND xid = ? AND recv_type = ?
		    AND action IN ('offer','ack','nak')
		  ORDER BY id DESC LIMIT 1`,
		clientKey, xid, recvType)
	var (
		id           int64
		createdAt    int64
		action       string
		replyType    string
		offeredIP    []byte
		leaseExpires int64
		refKind      string
		refID        int64
		reason       string
		replyBytes   []byte
	)
	if err := row.Scan(&id, &createdAt, &action, &replyType, &offeredIP, &leaseExpires,
		&refKind, &refID, &reason, &replyBytes); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	return &journalRow{
		ID:           id,
		createdAt:    createdAt,
		clientKey:    clientKey,
		xid:          xid,
		recvType:     recvType,
		action:       Action(action),
		replyType:    replyType,
		offeredIP:    offeredIP,
		leaseExpires: leaseExpires,
		refKind:      refKind,
		refID:        refID,
		reason:       reason,
		replyBytes:   replyBytes,
	}, nil
}

// markDupStaleLocked moves an obsolete dedup row out of the partial
// unique index (action no longer matches offer/ack/nak) while keeping it
// in the journal. Used when the state a duplicate reply referred to has
// since ended and the same xid must be processed as a fresh transaction.
func markDupStaleLocked(ctx context.Context, tx *rawTx, id int64) error {
	_, err := tx.Conn.ExecContext(ctx,
		`UPDATE replies SET action = action || '_stale' WHERE id = ?`, id)
	return err
}

// offerGenerationLiveLocked reports whether the SPECIFIC offer row the
// earlier OFFER granted is still live (unsuperseded and unexpired). It is
// deliberately keyed by the row id rather than by (client, IP): a newer
// DISCOVER supersedes the old offer row even when the same address is
// offered again, and a replayed old xid must not be answered as if its
// (now-dead) reservation were current.
func (s *Store) offerGenerationLiveLocked(ctx context.Context, tx *rawTx, offerID int64) bool {
	if offerID <= 0 {
		// Rows written before ref tracking: fall back to false so an old
		// transaction without a provenance is processed afresh.
		return false
	}
	var n int
	if err := tx.Conn.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM offers
		  WHERE id = ? AND superseded = 0 AND expires_at > ?`,
		offerID, s.now().UnixNano()).Scan(&n); err != nil {
		return false
	}
	return n == 1
}

// leaseGenerationLiveLocked reports whether the SPECIFIC lease row the
// earlier ACK granted is still live. A released/expired first lease must
// not be resurrected merely because the client later re-acquired the same
// address under a newer lease row.
func (s *Store) leaseGenerationLiveLocked(ctx context.Context, tx *rawTx, leaseID int64) bool {
	if leaseID <= 0 {
		return false
	}
	var n int
	if err := tx.Conn.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM leases WHERE id = ? AND state = 'leased'`,
		leaseID).Scan(&n); err != nil {
		return false
	}
	return n == 1
}
