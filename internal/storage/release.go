package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/netip"

	"dhcp4lab/internal/dhcp4"
)

// Release implements RFC 2131 §3/§4.4.4: the client relinquishes its
// lease. There is no reply. The row moves 'leased' -> 'released', which
// frees the IP through the partial unique index. A RELEASE for an address
// that is not the client's live lease is logged with a classified reason
// and still produces no reply (the client is, by definition, leaving).
func (s *Store) Release(ctx context.Context, in ReleaseInput) (*Outcome, error) {
	tx, err := s.beginImmediate(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()

	now := s.now()
	nowN := now.UnixNano()
	if _, _, err := s.sweepLocked(ctx, tx, nowN); err != nil {
		return nil, err
	}
	key := in.Identity.Key()

	// Defensive boundary: a RELEASE must name a valid non-zero ciaddr.
	// The adapter rejects this too; guarding here keeps the store safe
	// when driven directly (HTTP replay, tests, future transports).
	if !in.CIAddr.IsValid() || in.CIAddr.IsUnspecified() {
		if err := tx.Rollback(); err != nil {
			return nil, err
		}
		return &Outcome{
			Action: ActDrop,
			Reason: ReasonMalformed + ": release_without_ciaddr",
		}, nil
	}

	cid, err := s.upsertClientLocked(ctx, tx, in.Identity, nowN)
	if err != nil {
		return nil, err
	}

	var leaseID int64
	err = tx.Conn.QueryRowContext(ctx,
		`SELECT id FROM leases WHERE client_id = ? AND ip = ? AND state = 'leased'`,
		cid, ipBlob(in.CIAddr)).Scan(&leaseID)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		// No matching live lease: record a classified no-op. Common in
		// tests that replay a RELEASE twice (idempotence / unknown state
		// must never be reported as success for a lease that did not
		// exist).
		out := &Outcome{Action: ActDrop, Reason: ReasonReleaseNoMatch}
		if _, err := s.insertReplyLocked(ctx, tx, journalRow{
			createdAt: nowN, clientKey: key, xid: in.XID[:], recvType: "RELEASE",
			action: ActDrop, reason: ReasonReleaseNoMatch,
		}); err != nil {
			return nil, err
		}
		if err := s.insertEventLocked(ctx, tx, key, in.XID[:], "release_mismatch",
			fmt.Sprintf("release for %s matched no live lease", in.CIAddr)); err != nil {
			return nil, err
		}
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return out, nil
	case err != nil:
		return nil, err
	}

	if _, err := tx.Conn.ExecContext(ctx,
		`UPDATE leases SET state = 'released', ended_at = ?, note = 'client RELEASE'
		  WHERE id = ?`, nowN, leaseID); err != nil {
		return nil, err
	}
	if _, err := s.insertReplyLocked(ctx, tx, journalRow{
		createdAt: nowN, clientKey: key, xid: in.XID[:], recvType: "RELEASE",
		action: ActReleased, offeredIP: ipBlob(in.CIAddr), reason: "client released",
	}); err != nil {
		return nil, err
	}
	if err := s.insertEventLocked(ctx, tx, key, in.XID[:], "release",
		fmt.Sprintf("released %s", in.CIAddr)); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &Outcome{
		Action:     ActReleased,
		LeaseIP:    in.CIAddr,
		LeaseState: StateReleased,
	}, nil
}

// ClientLease returns the client's current lease view (any terminal
// state), or nil if the client has never had one.
func (s *Store) ClientLease(ctx context.Context, id dhcp4.ClientIdentity) (*LeaseView, error) {
	key := id.Key()
	var (
		ip      []byte
		state   string
		expires int64
		updated int64
		created int64
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT l.ip, l.state, l.expires_at, l.updated_at, l.created_at
		   FROM leases l JOIN clients c ON c.id = l.client_id
		  WHERE c.key = ?
		  ORDER BY l.id DESC LIMIT 1`, key).
		Scan(&ip, &state, &expires, &updated, &created)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &LeaseView{
		IP:        blobIP(ip),
		State:     LeaseState(state),
		ClientKey: key,
		ExpiresAt: unixNano(expires),
		UpdatedAt: unixNano(updated),
		CreatedAt: unixNano(created),
	}, nil
}

// ActiveOffer returns the live offer (reservation) for a client.
func (s *Store) ActiveOffer(ctx context.Context, id dhcp4.ClientIdentity) (*LeaseView, error) {
	key := id.Key()
	var ip []byte
	var expires, created int64
	err := s.db.QueryRowContext(ctx,
		`SELECT o.ip, o.expires_at, o.created_at
		   FROM offers o JOIN clients c ON c.id = o.client_id
		  WHERE c.key = ? AND o.superseded = 0 AND o.expires_at > ?
		  ORDER BY o.id DESC LIMIT 1`,
		key, s.now().UnixNano()).Scan(&ip, &expires, &created)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &LeaseView{
		IP:        blobIP(ip),
		State:     StateOffered,
		ClientKey: key,
		ExpiresAt: unixNano(expires),
		CreatedAt: unixNano(created),
	}, nil
}

// LeaseByIP returns the current row for an address in any state.
func (s *Store) LeaseByIP(ctx context.Context, a netip.Addr) (*LeaseView, error) {
	var (
		ip      []byte
		key     string
		state   string
		expires int64
		updated int64
		created int64
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT l.ip, c.key, l.state, l.expires_at, l.updated_at, l.created_at
		   FROM leases l JOIN clients c ON c.id = l.client_id
		  WHERE l.ip = ?
		  ORDER BY l.id DESC LIMIT 1`, ipBlob(a)).
		Scan(&ip, &key, &state, &expires, &updated, &created)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &LeaseView{
		IP:        blobIP(ip),
		State:     LeaseState(state),
		ClientKey: key,
		ExpiresAt: unixNano(expires),
		UpdatedAt: unixNano(updated),
		CreatedAt: unixNano(created),
	}, nil
}

// ListLeases returns the most recent row per IP address, newest first.
func (s *Store) ListLeases(ctx context.Context, limit int) ([]LeaseView, error) {
	if limit <= 0 {
		limit = 200
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT l.ip, c.key, l.state, l.expires_at, l.updated_at, l.created_at
		   FROM leases l JOIN clients c ON c.id = l.client_id
		  WHERE l.id IN (SELECT MAX(id) FROM leases GROUP BY ip)
		  ORDER BY l.id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []LeaseView
	for rows.Next() {
		var (
			ip, key, state            []byte
			expires, updated, created int64
		)
		if err := rows.Scan(&ip, &key, &state, &expires, &updated, &created); err != nil {
			return nil, err
		}
		out = append(out, LeaseView{
			IP: blobIP(ip), State: LeaseState(state), ClientKey: string(key),
			ExpiresAt: unixNano(expires), UpdatedAt: unixNano(updated), CreatedAt: unixNano(created),
		})
	}
	return out, rows.Err()
}
