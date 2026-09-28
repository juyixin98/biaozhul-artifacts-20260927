package server

import (
	"context"
	"time"

	"dhcpv4lab/internal/storage"
)

func orEmpty(s string) string {
	if s == "" {
		return "default"
	}
	return s
}

// recordEventXID persists one structured decision event. Failures are
// intentionally non-fatal to the protocol path but surfaced in counters via a
// follow-up log at the transport layer.
func (s *Server) recordEventXID(ctx context.Context, o *Outcome, xid uint32, inType byte,
	ident storage.Identity, mac string, src Source, assigned string, now time.Time) {
	ev := storage.Event{
		RunID:      orEmpty(src.RunID),
		TsNanos:    now.UnixNano(),
		XID:        xid,
		IdentityID: ident.Key,
		MAC:        mac,
		RemoteAddr: src.RemoteAddr,
		InType:     msgName(inType),
		OutType:    msgName(o.OutType),
		Action:     o.Action,
		Result:     string(o.Category),
		Reason:     o.Reason,
		Detail:     o.Detail,
		AssignedIP: assigned,
	}
	_, _ = s.store.InsertEvent(ctx, ev)
}
