package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/netip"

	"dhcp4lab/internal/dhcp4"
)

// Discover implements the server half of the INIT state (RFC 2131 §4.3.2):
// it reserves (never leases) an address as OFFER. A repeat DISCOVER with
// the same (identity, xid) re-sends the identical OFFER and does NOT move
// its expiry; a new DISCOVER transaction supersedes the previous offer.
func (s *Store) Discover(ctx context.Context, in DiscoverInput) (*Outcome, error) {
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

	// 1) Duplicate transaction: answer identically, do not touch state.
	//    The reply is replayed only while the SPECIFIC offer row it
	//    granted is still live. If that reservation expired or was
	//    superseded — including by a newer DISCOVER for the same address —
	//    retire the dedup row and process this datagram as a fresh
	//    transaction.
	dup, err := findDuplicateReplyLocked(ctx, tx, key, in.XID[:], "DISCOVER")
	if err != nil {
		return nil, err
	}
	if dup != nil && dup.action == ActOffer &&
		!s.offerGenerationLiveLocked(ctx, tx, dup.refID) {
		if err := markDupStaleLocked(ctx, tx, dup.ID); err != nil {
			return nil, err
		}
		dup = nil
	}
	if dup != nil {
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return s.duplicateOutcome(dup, in.CHAddr, in.ClientID), nil
	}

	cid, err := s.upsertClientLocked(ctx, tx, in.Identity, nowN)
	if err != nil {
		return nil, err
	}

	// 2) Existing live offer for this client? A new DISCOVER supersedes
	//    it; we prefer offering the SAME address again (RFC 2131: servers
	//    SHOULD be consistent) as long as it is still free.
	var existingIP []byte
	err = tx.Conn.QueryRowContext(ctx,
		`SELECT ip FROM offers WHERE client_id = ? AND superseded = 0`, cid).
		Scan(&existingIP)
	supersedeOld := false
	switch {
	case err == nil:
		supersedeOld = true
	case errors.Is(err, sql.ErrNoRows):
	default:
		return nil, err
	}

	var chosen netip.Addr
	haveChosen := false
	if supersedeOld && s.ipFreeLocked(ctx, tx, existingIP, cid) {
		chosen = blobIP(existingIP)
		haveChosen = true
	}
	if !haveChosen {
		// Prefer the client's last leased address (RFC SHOULD reuse).
		if last, ok, err := s.lastLeasedIPLocked(ctx, tx, cid); err != nil {
			return nil, err
		} else if ok && s.pool.Contains(blobIP(last)) && s.ipFreeLocked(ctx, tx, last, cid) {
			chosen = blobIP(last)
			haveChosen = true
		}
	}
	if !haveChosen {
		if a, ok := s.scanFreePool(ctx, tx, cid); ok {
			chosen = a
			haveChosen = true
		}
	}
	if !haveChosen {
		out := &Outcome{Action: ActPoolExhausted, Reason: ReasonPoolExhausted}
		if _, err := s.insertReplyLocked(ctx, tx, journalRow{
			createdAt: nowN, clientKey: key, xid: in.XID[:], recvType: "DISCOVER",
			action: ActPoolExhausted, reason: ReasonPoolExhausted,
		}); err != nil {
			return nil, err
		}
		if err := s.insertEventLocked(ctx, tx, key, in.XID[:], "discover_exhausted",
			"no free address in pool"); err != nil {
			return nil, err
		}
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return out, nil
	}

	expires := now.Add(s.offerTTL)
	if supersedeOld {
		if _, err := tx.Conn.ExecContext(ctx,
			`UPDATE offers SET superseded = 1, expires_at = ? WHERE client_id = ? AND superseded = 0`,
			nowN, cid); err != nil {
			return nil, err
		}
	}

	var offerID int64
	insertOffer := func() error {
		res, err := tx.Conn.ExecContext(ctx,
			`INSERT INTO offers(ip, client_id, xid, created_at, expires_at, superseded)
			 VALUES(?, ?, ?, ?, ?, 0)`,
			ipBlob(chosen), cid, in.XID[:], nowN, expires.UnixNano())
		if err != nil {
			return err
		}
		offerID, err = res.LastInsertId()
		return err
	}
	if err := insertOffer(); err != nil {
		// We hold the write lock, so this can only be a candidate lost to
		// an uncommitted-then-committed ordering bug; scan once more.
		if a, ok := s.scanFreePool(ctx, tx, cid); ok {
			chosen = a
			if err := insertOffer(); err != nil {
				return nil, fmt.Errorf("offer insert retry: %w", err)
			}
		} else {
			out := &Outcome{Action: ActPoolExhausted, Reason: ReasonPoolExhausted}
			if _, err := s.insertReplyLocked(ctx, tx, journalRow{
				createdAt: nowN, clientKey: key, xid: in.XID[:], recvType: "DISCOVER",
				action: ActPoolExhausted, reason: ReasonPoolExhausted,
			}); err != nil {
				return nil, err
			}
			if err := tx.Commit(); err != nil {
				return nil, err
			}
			return out, nil
		}
	}

	replySpec := &ReplySpec{
		Type:         dhcp4.MsgOffer,
		XID:          in.XID,
		YIAddr:       chosen,
		CHAddr:       in.CHAddr,
		ClientIDOpt:  append([]byte(nil), in.ClientID...),
		LeaseSeconds: uint32(s.leaseTime.Seconds()),
	}
	if _, err := s.insertReplyLocked(ctx, tx, journalRow{
		createdAt: nowN, clientKey: key, xid: in.XID[:], recvType: "DISCOVER",
		action: ActOffer, replyType: "OFFER", offeredIP: ipBlob(chosen),
		leaseExpires: expires.UnixNano(), refKind: "offer", refID: offerID,
		reason: "offer_reservation",
	}); err != nil {
		return nil, err
	}
	if err := s.insertEventLocked(ctx, tx, key, in.XID[:], "offer",
		fmt.Sprintf("reserved %s until %d (ttl=%s)", chosen, expires.UnixNano(), s.offerTTL)); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &Outcome{
		Action:       ActOffer,
		Reply:        replySpec,
		LeaseIP:      chosen,
		LeaseState:   StateOffered,
		LeaseExpires: expires,
	}, nil
}

// ipFreeLocked reports that ip is available to reserve for selfClient:
// no OTHER client holds a live offer or live lease for it. The client's
// own live offer/lease do not block re-offering it the same address
// (RFC: a server SHOULD return the client's current address when it
// re-DISCOVERs while holding a lease).
func (s *Store) ipFreeLocked(ctx context.Context, tx *rawTx, ip []byte, selfClient int64) bool {
	var nOffer int
	if err := tx.Conn.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM offers WHERE ip = ? AND superseded = 0 AND client_id != ?`,
		ip, selfClient).Scan(&nOffer); err != nil {
		return false
	}
	var nLease int
	if err := tx.Conn.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM leases WHERE ip = ? AND state = 'leased' AND client_id != ?`,
		ip, selfClient).Scan(&nLease); err != nil {
		return false
	}
	return nOffer == 0 && nLease == 0
}

func (s *Store) lastLeasedIPLocked(ctx context.Context, tx *rawTx, client int64) ([]byte, bool, error) {
	var ip []byte
	err := tx.Conn.QueryRowContext(ctx,
		`SELECT ip FROM leases WHERE client_id = ?
		  ORDER BY updated_at DESC, id DESC LIMIT 1`, client).Scan(&ip)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return ip, true, nil
}

// scanFreePool walks the pool inside the current write transaction and
// returns the first candidate that has neither a live offer by another
// client nor a live lease.
func (s *Store) scanFreePool(ctx context.Context, tx *rawTx, selfClient int64) (netip.Addr, bool) {
	found := false
	var out netip.Addr
	s.pool.Next(func(cand netip.Addr) bool {
		ip := ipBlob(cand)
		var nOffer, nLease int
		if err := tx.Conn.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM offers WHERE ip = ? AND superseded = 0 AND client_id != ?`,
			ip, selfClient).Scan(&nOffer); err != nil {
			return false
		}
		if err := tx.Conn.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM leases WHERE ip = ? AND state = 'leased'`, ip).Scan(&nLease); err != nil {
			return false
		}
		if nOffer == 0 && nLease == 0 {
			out = cand
			found = true
			return true
		}
		return false
	})
	return out, found
}
