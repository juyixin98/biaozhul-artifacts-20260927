package storage

import (
	"context"
	"database/sql"
	"errors"
	"strings"
)

// ActiveLease returns the single OFFERED/BOUND row for a client, if any.
// A stale row for the same client in a different state is ignored.
func (s *Store) ActiveLease(ctx context.Context, identityID string) (*Lease, error) {
	const q = `
		SELECT id, ip, identity_id, identity_label, state, xid,
		       offered_at, offer_exp, bound_at, starts, ends, renew_count, updated_at
		FROM leases
		WHERE identity_id = ? AND state IN ('OFFERED','BOUND')
		LIMIT 1`
	row := s.db.QueryRowContext(ctx, q, identityID)
	l, err := scanLease(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return l, err
}

// ActiveLeaseAtIP returns the active record occupying an address, if any.
func (s *Store) ActiveLeaseAtIP(ctx context.Context, ip string) (*Lease, error) {
	const q = `
		SELECT id, ip, identity_id, identity_label, state, xid,
		       offered_at, offer_exp, bound_at, starts, ends, renew_count, updated_at
		FROM leases
		WHERE ip = ? AND state IN ('OFFERED','BOUND')
		LIMIT 1`
	l, err := scanLease(s.db.QueryRowContext(ctx, q, ip))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return l, err
}

// LeaseAtIP returns the latest row (any state) for an address — used by
// diagnostics and INIT-REBOOT validation of prior configurations.
func (s *Store) LeaseAtIP(ctx context.Context, ip string) (*Lease, error) {
	const q = `
		SELECT id, ip, identity_id, identity_label, state, xid,
		       offered_at, offer_exp, bound_at, starts, ends, renew_count, updated_at
		FROM leases WHERE ip = ? ORDER BY id DESC LIMIT 1`
	l, err := scanLease(s.db.QueryRowContext(ctx, q, ip))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return l, err
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanLease(sc rowScanner) (*Lease, error) {
	var l Lease
	var state string
	err := sc.Scan(&l.ID, &l.IP, &l.IdentityID, &l.IdentityLabel, &state, &l.XID,
		&l.OfferedAt, &l.OfferExp, &l.BoundAt, &l.Starts, &l.Ends, &l.RenewCount, &l.UpdatedAt)
	if err != nil {
		return nil, err
	}
	l.State = LeaseState(state)
	return &l, nil
}

// ReserveOfferInput carries the DISCOVER decision into the atomic reserve.
type ReserveOfferInput struct {
	Identity      Identity
	XID           uint32
	IP            string
	NowNanos      int64
	OfferExpNanos int64
}

// ReserveOfferResult reports how the OFFER reservation landed.
type ReserveOfferResult struct {
	IP           string
	Created      bool   // new OFFERED row
	Reused       bool   // client's own existing reservation reused
	ReboundFrom  string // previous ip when an offer was re-targeted
	ContentionIP string // populated when the wanted IP was taken (Status=lost)
	Status       string // "ok" | "lost_to_other" | "pool_exhausted"
}

// ReserveOffer atomically creates or refreshes an OFFERED reservation.
//
// Invariant enforced at commit by idx_leases_active_ip: exactly one active
// record per address. The wantedIP may be empty, in which case the caller
// passes the chosen candidate; otherwise contention is reported back and the
// caller must select another candidate.
func (s *Store) ReserveOffer(ctx context.Context, in ReserveOfferInput) (*ReserveOfferResult, error) {
	res := &ReserveOfferResult{IP: in.IP}
	err := s.withImmediate(ctx, func(tx *sql.Tx) error {
		// Existing active record for this client?
		existing, err := queryActiveLeaseTx(ctx, tx, `identity_id = ?`, in.Identity.Key)
		if err != nil {
			return err
		}
		if existing != nil {
			if existing.State == StateBound {
				// Bound clients re-DISCOVERing are offered their leased
				// address (RFC 2131 §4.3.1); lease timings are untouched.
				res.IP = existing.IP
				res.Reused = true
				return nil
			}
			// OFFERED: refresh only expiry, and retarget when the wanted IP
			// changed and is still free.
			if in.IP != "" && in.IP != existing.IP {
				other, err := queryActiveLeaseTx(ctx, tx, `ip = ?`, in.IP)
				if err != nil {
					return err
				}
				if other != nil && other.IdentityID != in.Identity.Key {
					res.Status = "lost_to_other"
					res.ContentionIP = in.IP
					return nil
				}
				// Re-target the client's own offer: expire old row, insert new.
				if _, err := tx.ExecContext(ctx,
					`UPDATE leases SET state='EXPIRED', updated_at=? WHERE id=?`,
					in.NowNanos, existing.ID); err != nil {
					return err
				}
				res.ReboundFrom = existing.IP
				if err := insertOffered(ctx, tx, in); err != nil {
					if isUniqueConstraint(err) {
						res.Status = "lost_to_other"
						res.ContentionIP = in.IP
						return nil
					}
					return err
				}
				res.Created = true
				return nil
			}
			_, err := tx.ExecContext(ctx,
				`UPDATE leases SET offer_exp=?, updated_at=?, xid=? WHERE id=?`,
				in.OfferExpNanos, in.NowNanos, in.XID, existing.ID)
			if err != nil {
				return err
			}
			res.IP = existing.IP
			res.Reused = true
			return nil
		}

		if in.IP == "" {
			res.Status = "pool_exhausted"
			return nil
		}
		other, err := queryActiveLeaseTx(ctx, tx, `ip = ?`, in.IP)
		if err != nil {
			return err
		}
		if other != nil {
			res.Status = "lost_to_other"
			res.ContentionIP = in.IP
			return nil
		}
		if err := insertOffered(ctx, tx, in); err != nil {
			if isUniqueConstraint(err) {
				res.Status = "lost_to_other"
				res.ContentionIP = in.IP
				return nil
			}
			return err
		}
		res.Created = true
		return nil
	})
	if err != nil {
		return nil, err
	}
	if res.Status == "" {
		res.Status = "ok"
	}
	return res, nil
}

func insertOffered(ctx context.Context, tx *sql.Tx, in ReserveOfferInput) error {
	_, err := tx.ExecContext(ctx, `
		INSERT INTO leases(ip, identity_id, identity_label, state, xid,
		                   offered_at, offer_exp, updated_at)
		VALUES (?, ?, ?, 'OFFERED', ?, ?, ?, ?)`,
		in.IP, in.Identity.Key, in.Identity.Label, in.XID,
		in.NowNanos, in.OfferExpNanos, in.NowNanos)
	return err
}

func queryActiveLeaseTx(ctx context.Context, tx *sql.Tx, where string, arg string) (*Lease, error) {
	q := `SELECT id, ip, identity_id, identity_label, state, xid,
	             offered_at, offer_exp, bound_at, starts, ends, renew_count, updated_at
	      FROM leases WHERE ` + where + ` AND state IN ('OFFERED','BOUND') LIMIT 1`
	row := tx.QueryRowContext(ctx, q, arg)
	l, err := scanLease(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return l, err
}

func isUniqueConstraint(err error) bool {
	return err != nil && (strings.Contains(err.Error(), "UNIQUE constraint") ||
		strings.Contains(err.Error(), "unique constraint"))
}

// CommitAckInput carries the REQUEST decision.
type CommitAckInput struct {
	Identity      Identity
	XID           uint32
	IP            string
	NowNanos      int64
	StartNanos    int64
	EndNanos      int64
	RenewExisting bool // true for renew/rebind against the current BOUND row
}

// CommitAckResult describes the commit outcome for REQUEST.
type CommitAckResult struct {
	Status string // "committed" | "renewed" | "lost_to_other"
	Lease  *Lease
}

// CommitAck atomically turns an OFFERED (or, for renew, BOUND) row into a
// committed BOUND lease. If another client owns the address concurrently,
// Status=lost_to_other is returned and no state changes occur.
func (s *Store) CommitAck(ctx context.Context, in CommitAckInput) (*CommitAckResult, error) {
	out := &CommitAckResult{}
	err := s.withImmediate(ctx, func(tx *sql.Tx) error {
		other, err := queryActiveLeaseTx(ctx, tx, `ip = ?`, in.IP)
		if err != nil {
			return err
		}
		if other != nil && other.IdentityID != in.Identity.Key {
			out.Status = "lost_to_other"
			return nil
		}

		if in.RenewExisting {
			if other == nil || other.State != StateBound {
				// Lease vanished (expired/released) between check and commit:
				// caller must NAK.
				out.Status = "not_renewable"
				return nil
			}
			res, err := tx.ExecContext(ctx, `
				UPDATE leases
				SET starts=?, ends=?, renew_count=renew_count+1, updated_at=?
				WHERE id=? AND state='BOUND'`,
				in.StartNanos, in.EndNanos, in.NowNanos, other.ID)
			if err != nil {
				return err
			}
			n, _ := res.RowsAffected()
			if n == 0 {
				return ErrLeaseGone
			}
			out.Status = "renewed"
			out.Lease, err = leaseByIDTx(ctx, tx, other.ID)
			if err != nil {
				return err
			}
			return nil
		}

		// INIT/REBOOT or selecting case: own OFFERED row (or an own BOUND row
		// for a re-REQUEST) becomes BOUND.
		var target *Lease
		if other != nil && other.IdentityID == in.Identity.Key {
			target = other
		} else {
			target, err = queryActiveLeaseTx(ctx, tx, `identity_id = ?`, in.Identity.Key)
			if err != nil {
				return err
			}
		}
		if target == nil || target.IP != in.IP {
			out.Status = "lost_to_other"
			return nil
		}
		res, err := tx.ExecContext(ctx, `
			UPDATE leases
			SET state='BOUND', bound_at=?, starts=?, ends=?,
			    renew_count=0, updated_at=?, offer_exp=0, xid=?
			WHERE id=? AND state IN ('OFFERED','BOUND')`,
			in.NowNanos, in.StartNanos, in.EndNanos, in.NowNanos, in.XID, target.ID)
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			out.Status = "lost_to_other"
			return nil
		}
		out.Status = "committed"
		l, err := leaseByIDTx(ctx, tx, target.ID)
		if err != nil {
			return err
		}
		out.Lease = l
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func leaseByIDTx(ctx context.Context, tx *sql.Tx, id int64) (*Lease, error) {
	q := `SELECT id, ip, identity_id, identity_label, state, xid,
	             offered_at, offer_exp, bound_at, starts, ends, renew_count, updated_at
	      FROM leases WHERE id = ?`
	l, err := scanLease(tx.QueryRowContext(ctx, q, id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrLeaseGone
	}
	return l, err
}

// ReleaseLease marks a client's BOUND lease at ip RELEASED atomically.
// Returns:
//
//	released=true on the first release;
//	alreadyGone=true for a duplicate RELEASE / expired/non-bound row;
//	notOwner=true when the client never owned the address.
func (s *Store) ReleaseLease(ctx context.Context, identityID, ip string, nowNanos int64) (released, alreadyGone, notOwner bool, err error) {
	err = s.withImmediate(ctx, func(tx *sql.Tx) error {
		var id int64
		var state, owner string
		row := tx.QueryRowContext(ctx,
			`SELECT id, state, identity_id FROM leases WHERE ip=? ORDER BY id DESC LIMIT 1`, ip)
		if scanErr := row.Scan(&id, &state, &owner); scanErr != nil {
			if errors.Is(scanErr, sql.ErrNoRows) {
				notOwner = true
				return nil
			}
			return scanErr
		}
		if owner != identityID {
			notOwner = true
			return nil
		}
		if state != "BOUND" {
			alreadyGone = true
			return nil
		}
		if _, execErr := tx.ExecContext(ctx,
			`UPDATE leases SET state='RELEASED', updated_at=? WHERE id=? AND state='BOUND'`,
			nowNanos, id); execErr != nil {
			return execErr
		}
		released = true
		return nil
	})
	return
}

// SweepExpired transitions timed-out OFFER rows and elapsed BOUND leases to
// EXPIRED. It returns the touched IPs and new state, so the caller can emit
// one diagnostic event per expiry.
func (s *Store) SweepExpired(ctx context.Context, nowNanos int64) ([]SweepChange, error) {
	var changes []SweepChange
	err := s.withImmediate(ctx, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `
			SELECT id, ip, state, identity_id FROM leases
			WHERE state='OFFERED' AND offer_exp <= ?
			   OR state='BOUND'   AND ends      <= ?`,
			nowNanos, nowNanos)
		if err != nil {
			return err
		}
		type pending struct {
			id    int64
			ip    string
			state string
			ident string
		}
		var pend []pending
		for rows.Next() {
			var p pending
			if err := rows.Scan(&p.id, &p.ip, &p.state, &p.ident); err != nil {
				rows.Close()
				return err
			}
			pend = append(pend, p)
		}
		rows.Close()
		for _, p := range pend {
			if _, err := tx.ExecContext(ctx,
				`UPDATE leases SET state='EXPIRED', updated_at=? WHERE id=?`,
				nowNanos, p.id); err != nil {
				return err
			}
			changes = append(changes, SweepChange{IP: p.ip, From: LeaseState(p.state), To: StateExpired, IdentityID: p.ident})
		}
		return nil
	})
	return changes, err
}

// SweepChange records one expiry transition.
type SweepChange struct {
	IP         string     `json:"ip"`
	From       LeaseState `json:"from"`
	To         LeaseState `json:"to"`
	IdentityID string     `json:"identityId"`
}

// ListLeases returns lease rows for diagnostics ordered by id.
func (s *Store) ListLeases(ctx context.Context, limit int) ([]Lease, error) {
	if limit <= 0 || limit > 10000 {
		limit = 200
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, ip, identity_id, identity_label, state, xid,
		       offered_at, offer_exp, bound_at, starts, ends, renew_count, updated_at
		FROM leases ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Lease
	for rows.Next() {
		l, err := scanLease(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *l)
	}
	return out, rows.Err()
}

// ActiveAddressCount returns (offered, bound) counts for pool occupancy.
func (s *Store) ActiveAddressCount(ctx context.Context) (offered, bound int64, err error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT state, COUNT(*) FROM leases WHERE state IN ('OFFERED','BOUND') GROUP BY state`)
	if err != nil {
		return 0, 0, err
	}
	defer rows.Close()
	for rows.Next() {
		var st string
		var n int64
		if err := rows.Scan(&st, &n); err != nil {
			return 0, 0, err
		}
		switch LeaseState(st) {
		case StateOffered:
			offered = n
		case StateBound:
			bound = n
		}
	}
	return offered, bound, rows.Err()
}
