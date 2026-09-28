package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/netip"
	"strings"
	"time"

	"dhcp4lab/internal/dhcp4"
)

// Request handles one REQUEST according to its classified kind:
//
//   - selecting   (option 54): client chose a server. ACK only against a
//     live OFFER made to this identity for requested IP;
//     NAK otherwise (RFC 2131 §4.3.2).
//   - init-reboot (opt50, no 54, ciaddr=0): verify client is known and
//     the address is correct for its lease; NAK on mismatch,
//     stay silent for unknown clients.
//   - renew       (ciaddr!=0, no 54): extend the lease that matches
//     ciaddr; stay silent when there is nothing to renew.
//
// Duplicate REQUEST transactions (same identity+xid) re-send the previous
// reply and never extend the lease.
func (s *Store) Request(ctx context.Context, in RequestInput) (*Outcome, error) {
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

	// Duplicate transaction handling. An ACK replay is honoured verbatim
	// (with the ORIGINAL expiry) only while that lease is still live; a
	// NAK replay is always repeated.
	dup, err := findDuplicateReplyLocked(ctx, tx, key, in.XID[:], "REQUEST")
	if err != nil {
		return nil, err
	}
	if dup != nil {
		stale := false
		switch dup.action {
		case ActACK:
			// An ACK can be replayed only while the SPECIFIC lease row
			// it granted is still live — not merely while some newer
			// lease for the same IP exists.
			stale = !s.leaseGenerationLiveLocked(ctx, tx, dup.refID)
		case ActNAK:
			// A NAK can be replayed only while the negative condition
			// that caused it still holds. If a success precondition has
			// since appeared (a live offer for selecting; a matching live
			// lease for init-reboot/renew), the old NAK is stale and the
			// request must be processed as a fresh transaction.
			stale = s.nakPreconditionClearedLocked(ctx, tx, dup, in)
		}
		if stale {
			// Retire the dedup row so the datagram is handled afresh
			// rather than resurrecting/replying an obsolete answer.
			if err := markDupStaleLocked(ctx, tx, dup.ID); err != nil {
				return nil, err
			}
			dup = nil
		}
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

	var out *Outcome
	switch in.Kind {
	case ReqSelecting:
		out, err = s.requestSelecting(ctx, tx, in, cid, now)
	case ReqInitReboot:
		out, err = s.requestInitReboot(ctx, tx, in, cid, now)
	case ReqRenew:
		out, err = s.requestRenew(ctx, tx, in, cid, now)
	default:
		return nil, fmt.Errorf("storage: unknown request kind %q", in.Kind)
	}
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return out, nil
}

func (s *Store) requestSelecting(ctx context.Context, tx *rawTx, in RequestInput, cid int64, now time.Time) (*Outcome, error) {
	nowN := now.UnixNano()
	key := in.Identity.Key()

	// Client selected a DIFFERENT server: protocol says ignore entirely.
	if in.ServerID.IsValid() && in.ServerID != s.server {
		return s.journalDrop(ctx, tx, nowN, key, in.XID[:], "REQUEST",
			ReasonForeignServerID,
			fmt.Sprintf("server_id=%s, this server=%s", in.ServerID, s.server))
	}

	// Find the live offer for (client, requested IP, this xid).
	var offerID int64
	err := tx.Conn.QueryRowContext(ctx,
		`SELECT id FROM offers
		  WHERE client_id = ? AND ip = ? AND xid = ?
		    AND superseded = 0 AND expires_at > ?`,
		cid, ipBlob(in.RequestedIP), in.XID[:], nowN).Scan(&offerID)
	if errors.Is(err, sql.ErrNoRows) {
		// Distinguish "never offered this" from "an offer for a different
		// IP exists" for precise diagnostics.
		var otherIP []byte
		err2 := tx.Conn.QueryRowContext(ctx,
			`SELECT ip FROM offers WHERE client_id = ? AND superseded = 0 AND xid = ?`,
			cid, in.XID[:]).Scan(&otherIP)
		reason := ReasonNoActiveOffer
		detail := fmt.Sprintf("requested=%s xid=% x", in.RequestedIP, in.XID)
		if err2 == nil {
			reason = ReasonOfferForOtherIP
			detail = fmt.Sprintf("offered=%s but requested=%s", blobIP(otherIP), in.RequestedIP)
		}
		return s.journalNAK(ctx, tx, nowN, key, in, reason, detail)
	}
	if err != nil {
		return nil, err
	}

	expires, leaseID, err := s.commitLeaseLocked(ctx, tx, cid, in.RequestedIP, in.XID[:], offerID, now)
	if err != nil {
		return nil, err
	}
	return s.journalACK(ctx, tx, nowN, key, in, cid, in.RequestedIP, expires, leaseID, "selected")
}

func (s *Store) requestInitReboot(ctx context.Context, tx *rawTx, in RequestInput, cid int64, now time.Time) (*Outcome, error) {
	nowN := now.UnixNano()
	key := in.Identity.Key()

	var leaseIP []byte
	err := tx.Conn.QueryRowContext(ctx,
		`SELECT ip FROM leases WHERE client_id = ? AND state = 'leased'`, cid).Scan(&leaseIP)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		// RFC 2131 §4.3.2: unknown client -> remain silent. Also silent
		// for a known client whose lease already ended (reboot after
		// expiry): it must re-DISCOVER.
		return s.journalDrop(ctx, tx, nowN, key, in.XID[:], "REQUEST",
			ReasonUnknownClientReboot,
			fmt.Sprintf("init-reboot requested=%s but no live lease for client", in.RequestedIP))
	case err != nil:
		return nil, err
	}

	if string(leaseIP) != string(ipBlob(in.RequestedIP)) {
		return s.journalNAK(ctx, tx, nowN, key, in, ReasonWrongIPInitReboot,
			fmt.Sprintf("client rebooted claiming %s, lease is %s", in.RequestedIP, blobIP(leaseIP)))
	}
	// Correct address confirmed: re-ACK and (re)start the lease timer.
	expires, leaseID, err := s.extendLeaseLocked(ctx, tx, cid, leaseIP, now)
	if err != nil {
		return nil, err
	}
	return s.journalACK(ctx, tx, nowN, key, in, cid, blobIP(leaseIP), expires, leaseID, "init-reboot")
}

func (s *Store) requestRenew(ctx context.Context, tx *rawTx, in RequestInput, cid int64, now time.Time) (*Outcome, error) {
	nowN := now.UnixNano()
	key := in.Identity.Key()

	var leaseIP []byte
	err := tx.Conn.QueryRowContext(ctx,
		`SELECT ip FROM leases WHERE client_id = ? AND state = 'leased'`, cid).Scan(&leaseIP)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		// RFC: no binding -> server remains silent (client retries until
		// rebinding, where another server may answer).
		return s.journalDrop(ctx, tx, nowN, key, in.XID[:], "REQUEST",
			ReasonRenewNoLease,
			fmt.Sprintf("renew ciaddr=%s but client has no live lease", in.CIAddr))
	case err != nil:
		return nil, err
	}

	if string(leaseIP) != string(ipBlob(in.CIAddr)) {
		return s.journalNAK(ctx, tx, nowN, key, in, ReasonRenewAddrMismatch,
			fmt.Sprintf("renew ciaddr=%s but live lease is %s", in.CIAddr, blobIP(leaseIP)))
	}
	expires, leaseID, err := s.extendLeaseLocked(ctx, tx, cid, leaseIP, now)
	if err != nil {
		return nil, err
	}
	return s.journalACK(ctx, tx, nowN, key, in, cid, blobIP(leaseIP), expires, leaseID, "renew")
}

// nakPreconditionClearedLocked reports whether the negative condition
// that produced a previous NAK has since been resolved, i.e. the success
// precondition for the current (kind, address) now holds. When true the
// stale NAK must not be replayed.
func (s *Store) nakPreconditionClearedLocked(ctx context.Context, tx *rawTx, dup *journalRow, in RequestInput) bool {
	nowN := s.now().UnixNano()
	switch {
	case strings.HasPrefix(dup.reason, ReasonNoActiveOffer),
		strings.HasPrefix(dup.reason, ReasonOfferForOtherIP):
		// SELECTING: a live offer for this client/xid/requested IP now
		// exists (and is unexpired).
		var n int
		if err := tx.Conn.QueryRowContext(ctx,
			`SELECT COUNT(*)
			   FROM offers o JOIN clients c ON c.id = o.client_id
			  WHERE c.key = ? AND o.xid = ? AND o.ip = ?
			    AND o.superseded = 0 AND o.expires_at > ?`,
			in.Identity.Key(), in.XID[:], ipBlob(in.RequestedIP), nowN).Scan(&n); err != nil {
			return false
		}
		return n > 0

	case strings.HasPrefix(dup.reason, ReasonWrongIPInitReboot):
		// INIT-REBOOT: the client's live lease is now exactly the
		// requested address.
		return s.clientLiveLeaseIPLocked(ctx, tx, in.Identity.Key()) == in.RequestedIP.String()

	case strings.HasPrefix(dup.reason, ReasonRenewAddrMismatch):
		// RENEW: the client's live lease now matches ciaddr.
		return s.clientLiveLeaseIPLocked(ctx, tx, in.Identity.Key()) == in.CIAddr.String()

	default:
		// Unrecognized NAK reason: conservatively keep replaying it.
		return false
	}
}

// clientLiveLeaseIPLocked returns the client's current live lease IP as
// a dotted-quad, or "" if none.
func (s *Store) clientLiveLeaseIPLocked(ctx context.Context, tx *rawTx, clientKey string) string {
	var ip []byte
	err := tx.Conn.QueryRowContext(ctx,
		`SELECT l.ip
		   FROM leases l JOIN clients c ON c.id = l.client_id
		  WHERE c.key = ? AND l.state = 'leased'
		  ORDER BY l.id DESC LIMIT 1`, clientKey).Scan(&ip)
	if err != nil || len(ip) != 4 {
		return ""
	}
	return blobIP(ip).String()
}

// commitLeaseLocked turns an accepted offer into a live lease atomically:
// the offer is marked superseded, any previous live lease for the client
// is ended ('released', forced by the per-client unique live-lease index),
// and the new leased row is inserted with a fresh expiry.
func (s *Store) commitLeaseLocked(ctx context.Context, tx *rawTx, cid int64, ip netip.Addr, xid []byte, offerID int64, now time.Time) (time.Time, int64, error) {
	nowN := now.UnixNano()
	expires := now.Add(s.leaseTime)

	if _, err := tx.Conn.ExecContext(ctx,
		`UPDATE offers SET superseded = 1, expires_at = ? WHERE id = ?`, nowN, offerID); err != nil {
		return time.Time{}, 0, err
	}
	// Defensively supersede all live offers for this client (a client
	// selecting address A must not keep a reservation for B).
	if _, err := tx.Conn.ExecContext(ctx,
		`UPDATE offers SET superseded = 1, expires_at = ? WHERE client_id = ? AND superseded = 0`,
		nowN, cid); err != nil {
		return time.Time{}, 0, err
	}
	if _, err := tx.Conn.ExecContext(ctx,
		`UPDATE leases SET state = 'released', ended_at = ?, note = 'superseded by new selection'
		  WHERE client_id = ? AND state = 'leased'`, nowN, cid); err != nil {
		return time.Time{}, 0, err
	}
	res, err := tx.Conn.ExecContext(ctx,
		`INSERT INTO leases(ip, client_id, state, xid, created_at, updated_at, expires_at, version)
		 VALUES(?, ?, 'leased', ?, ?, ?, ?, 1)`,
		ipBlob(ip), cid, xid, nowN, nowN, expires.UnixNano())
	if err != nil {
		return time.Time{}, 0, fmt.Errorf("commit lease: %w", err)
	}
	leaseID, err := res.LastInsertId()
	if err != nil {
		return time.Time{}, 0, err
	}
	return expires, leaseID, nil
}

// extendLeaseLocked moves the expiry of an existing live lease to
// now+leaseTime and bumps its version (optimistic marker). It returns the
// extended row's id so the caller can pin the ACK to this generation.
func (s *Store) extendLeaseLocked(ctx context.Context, tx *rawTx, cid int64, ip []byte, now time.Time) (time.Time, int64, error) {
	nowN := now.UnixNano()
	expires := now.Add(s.leaseTime)
	res, err := tx.Conn.ExecContext(ctx,
		`UPDATE leases SET updated_at = ?, expires_at = ?, version = version + 1
		  WHERE client_id = ? AND ip = ? AND state = 'leased'`,
		nowN, expires.UnixNano(), cid, ip)
	if err != nil {
		return time.Time{}, 0, err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return time.Time{}, 0, errors.New("extend lease: row vanished mid-transaction")
	}
	var leaseID int64
	if err := tx.Conn.QueryRowContext(ctx,
		`SELECT id FROM leases WHERE client_id = ? AND ip = ? AND state = 'leased'`,
		cid, ip).Scan(&leaseID); err != nil {
		return time.Time{}, 0, err
	}
	return expires, leaseID, nil
}

// journalACK records the ACK and builds its outcome/reply spec. leaseID
// pins the reply to the exact lease generation granted.
func (s *Store) journalACK(ctx context.Context, tx *rawTx, nowN int64, key string,
	in RequestInput, cid int64, ip netip.Addr, expires time.Time, leaseID int64, mode string) (*Outcome, error) {
	if _, err := s.insertReplyLocked(ctx, tx, journalRow{
		createdAt: nowN, clientKey: key, xid: in.XID[:], recvType: "REQUEST",
		action: ActACK, replyType: "ACK", offeredIP: ipBlob(ip),
		leaseExpires: expires.UnixNano(), refKind: "lease", refID: leaseID, reason: mode,
	}); err != nil {
		return nil, err
	}
	if err := s.insertEventLocked(ctx, tx, key, in.XID[:], "ack",
		fmt.Sprintf("committed lease %s mode=%s expires=%d", ip, mode, expires.UnixNano())); err != nil {
		return nil, err
	}
	return &Outcome{
		Action: ActACK,
		Reply: &ReplySpec{
			Type: dhcp4.MsgACK, XID: in.XID, YIAddr: ip,
			CHAddr: in.CHAddr, ClientIDOpt: append([]byte(nil), in.ClientID...),
			LeaseSeconds:    uint32(s.leaseTime.Seconds()),
			OriginalExpires: expires,
		},
		LeaseIP: ip, LeaseState: StateLeased, LeaseExpires: expires,
	}, nil
}

// journalNAK records and describes a NAK. A NAK carries only type 53 and
// server id on the wire; the reason is for the journal/diagnostics.
func (s *Store) journalNAK(ctx context.Context, tx *rawTx, nowN int64, key string,
	in RequestInput, reason, detail string) (*Outcome, error) {
	if _, err := s.insertReplyLocked(ctx, tx, journalRow{
		createdAt: nowN, clientKey: key, xid: in.XID[:], recvType: "REQUEST",
		action: ActNAK, replyType: "NAK", reason: reason + ": " + detail,
	}); err != nil {
		return nil, err
	}
	if err := s.insertEventLocked(ctx, tx, key, in.XID[:], "nak", reason+": "+detail); err != nil {
		return nil, err
	}
	return &Outcome{
		Action: ActNAK,
		Reply: &ReplySpec{
			Type: dhcp4.MsgNAK, XID: in.XID,
			CHAddr: in.CHAddr, ClientIDOpt: append([]byte(nil), in.ClientID...),
		},
		Reason: reason,
	}, nil
}

// journalDrop records a deliberate, protocol-correct silence.
func (s *Store) journalDrop(ctx context.Context, tx *rawTx, nowN int64, key string, xid []byte,
	recvType, reason, detail string) (*Outcome, error) {
	if _, err := s.insertReplyLocked(ctx, tx, journalRow{
		createdAt: nowN, clientKey: key, xid: xid, recvType: recvType,
		action: ActDrop, reason: reason + ": " + detail,
	}); err != nil {
		return nil, err
	}
	if err := s.insertEventLocked(ctx, tx, key, xid, "drop", reason+": "+detail); err != nil {
		return nil, err
	}
	return &Outcome{Action: ActDrop, Reason: reason}, nil
}

// duplicateOutcome reconstructs the exact previous response for a repeated
// transaction. For a previous ACK the carried lease time is the REMAINING
// time up to the original expiry (never re-extended); the journal's
// stored expiry proves stability.
func (s *Store) duplicateOutcome(dup *journalRow, chaddr [6]byte, clientID []byte) *Outcome {
	out := &Outcome{Duplicate: true, Reason: ReasonDuplicate}
	mt := dhcp4.MsgACK
	switch dup.replyType {
	case "OFFER":
		out.Action = ActOffer
		mt = dhcp4.MsgOffer
	case "ACK":
		out.Action = ActACK
		mt = dhcp4.MsgACK
	case "NAK":
		out.Action = ActNAK
		mt = dhcp4.MsgNAK
	default:
		// Should never happen: dedup only stores offer/ack/nak rows.
		out.Action = ActDrop
		return out
	}
	var xid [4]byte
	copy(xid[:], dup.xid)
	spec := &ReplySpec{
		Type: mt, XID: xid,
		CHAddr:      chaddr,
		ClientIDOpt: append([]byte(nil), clientID...),
	}
	if len(dup.offeredIP) == 4 {
		spec.YIAddr = blobIP(dup.offeredIP)
		out.LeaseIP = spec.YIAddr
	}
	if dup.leaseExpires > 0 {
		exp := time.Unix(0, dup.leaseExpires).UTC()
		spec.OriginalExpires = exp
		out.LeaseExpires = exp
		if mt == dhcp4.MsgACK {
			// Report the REMAINING lifetime up to the unchanged expiry.
			// The caller only reaches here when that exact lease row is
			// still live, so remaining is positive; round up to whole
			// seconds but never invent extra time beyond the expiry.
			remaining := exp.Sub(s.now())
			if remaining < 0 {
				remaining = 0
			}
			spec.LeaseSeconds = uint32((remaining + time.Second - 1) / time.Second)
			out.LeaseState = StateLeased
		} else {
			spec.LeaseSeconds = uint32(s.leaseTime.Seconds())
			out.LeaseState = StateOffered
		}
	}
	out.Reply = spec
	return out
}
