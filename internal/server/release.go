package server

import (
	"context"
	"fmt"
	"time"

	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/storage"
)

func (s *Server) handleRelease(ctx context.Context, p *dhcppacket.Packet,
	ident storage.Identity, _ Source, now time.Time) handled {

	// RFC 2131 §4.4.4: ciaddr names the address being released and the server
	// does not reply. Release only succeeds for the address's current owner;
	// a stranger's release never mutates someone else's lease.
	targetIP := p.CIAddr.Unmap()

	invalid := func(reason, detailFmt string, args ...any) handled {
		return handled{
			ident: ident, msgType: dhcppacket.MsgRelease,
			out: &Outcome{
				Category: storage.CatNoReply, Action: "release_rejected", Result: "no_reply",
				Reason: reason, Detail: fmt.Sprintf(detailFmt, args...),
			},
		}
	}

	if !targetIP.IsValid() || targetIP.IsUnspecified() {
		return invalid("release_missing_ciaddr",
			"RELEASE without ciaddr cannot identify a lease; no state changed")
	}
	if !s.pool.Contains(targetIP) {
		return invalid("release_outside_pool",
			"RELEASE ciaddr %s is outside the pool; no state changed", targetIP)
	}

	released, alreadyGone, notOwner, err := s.store.ReleaseLease(ctx, ident.Key, targetIP.String(), now.UnixNano())
	if err != nil {
		return handled{
			ident: ident, msgType: dhcppacket.MsgRelease,
			out: fail(storage.CatInternal, "release_failed", "%v", err),
		}
	}
	switch {
	case notOwner:
		return handled{
			ident: ident, msgType: dhcppacket.MsgRelease, assignedIP: targetIP.String(),
			out: &Outcome{
				Category: storage.CatNoReply, Action: "release_not_owner", Result: "no_reply",
				Reason: "release_not_owner",
				Detail: fmt.Sprintf("RELEASE for %s from non-owner %s; lease untouched",
					targetIP, ident.Label),
			},
		}
	case alreadyGone:
		return handled{
			ident: ident, msgType: dhcppacket.MsgRelease, assignedIP: targetIP.String(),
			out: &Outcome{
				Category: storage.CatNoReply, Action: "release_duplicate", Result: "no_reply",
				Reason:    "lease_already_released_or_expired",
				Detail:    fmt.Sprintf("duplicate/expired RELEASE for %s (idempotent, no reply per RFC 2131)", targetIP),
				Duplicate: true,
			},
		}
	case released:
		return handled{
			ident: ident, msgType: dhcppacket.MsgRelease, assignedIP: targetIP.String(),
			out: &Outcome{
				Category: storage.CatOK, Action: "release_ok", Result: "ok",
				Reason: "lease_released",
				Detail: fmt.Sprintf("BOUND lease at %s released by owner; address returned to pool (no reply)",
					targetIP),
			},
		}
	}
	// Unreachable: ReleaseLease sets exactly one of released/alreadyGone/notOwner.
	return handled{
		ident: ident, msgType: dhcppacket.MsgRelease,
		out: fail(storage.CatInternal, "release_indeterminate", "storage returned no release disposition"),
	}
}
