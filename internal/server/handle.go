package server

import (
	"context"
	"fmt"
	"net/netip"

	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/storage"
)

// handled is the internal return type of message branches: the outcome plus
// context the central dispatcher needs to persist one decision event.
type handled struct {
	out        *Outcome
	ident      storage.Identity
	msgType    byte
	assignedIP string
}

// Handle processes one decoded client datagram. The caller is responsible for
// UDP framing; Handle never panics on hostile input and classifies every
// outcome explicitly (it never reports success for an unknown state).
func (s *Server) Handle(ctx context.Context, p *dhcppacket.Packet, src Source) *Outcome {
	now := s.clock.Now()
	mac := fmt.Sprintf("%x", p.CHAddr)

	finish := func(h handled) *Outcome {
		s.recordEventXID(ctx, h.out, p.XID, h.msgType, h.ident, mac, src, h.assignedIP, now)
		switch h.out.Category {
		case storage.CatOK, storage.CatNAK, storage.CatNoReply:
			s.accepted.Add(1)
		default:
			s.rejected.Add(1)
		}
		if h.out.Duplicate {
			s.replayed.Add(1)
		}
		return h.out
	}

	if p.Op != dhcppacket.OpBootRequest {
		return finish(handled{
			out:     fail(storage.CatMalformed, "drop_not_bootrequest", "op=%d is not BOOTREQUEST; server only processes client requests", p.Op),
			msgType: 0,
		})
	}
	if p.GIAddr.IsValid() && !p.GIAddr.IsUnspecified() {
		return finish(handled{
			out:     fail(storage.CatMalformed, "drop_giaddr_set", "giaddr=%s present but no relay agent exists in the local lab", p.GIAddr),
			msgType: 0,
		})
	}

	msgType, err := p.MessageType()
	if err != nil {
		return finish(handled{
			out:     fail(storage.CatMalformed, "missing_message_type", "%v", err),
			msgType: 0,
		})
	}
	switch msgType {
	case dhcppacket.MsgDiscover, dhcppacket.MsgRequest, dhcppacket.MsgRelease:
		// supported
	default:
		return finish(handled{
			out: fail(storage.CatUnsupported, "unsupported_message_type",
				"message type %d (%s) is outside the implemented subset {DISCOVER,REQUEST,RELEASE}",
				msgType, orLabel(knownMsgName(msgType), "UNKNOWN")),
			msgType: msgType,
		})
	}

	ident := s.identity(p)
	fp := fingerprint(p, msgType)

	// Idempotency: identical retransmissions replay the stored reply byte for
	// byte and perform zero state mutation. RELEASE is idempotent through the
	// state table and is handled inside its own branch.
	if msgType != dhcppacket.MsgRelease {
		if rec, lerr := s.store.LookupTransaction(ctx, ident.Key, p.XID, fp); lerr != nil {
			return finish(handled{
				out:     fail(storage.CatInternal, "tx_lookup_failed", "%v", lerr),
				ident:   ident,
				msgType: msgType,
			})
		} else if rec != nil {
			return finish(handled{
				out:        s.replayOutcome(rec),
				ident:      ident,
				msgType:    msgType,
				assignedIP: rec.AssignedIP,
			})
		}
	}

	var h handled
	switch msgType {
	case dhcppacket.MsgDiscover:
		h = s.handleDiscover(ctx, p, ident, fp, src, now)
	case dhcppacket.MsgRequest:
		h = s.handleRequest(ctx, p, ident, fp, src, now)
	default:
		h = s.handleRelease(ctx, p, ident, src, now)
	}
	if h.ident.Key == "" {
		h.ident = ident
	}
	if h.msgType == 0 {
		h.msgType = msgType
	}
	return finish(h)
}

func orLabel(s, fallback string) string {
	if s != "" {
		return s
	}
	return fallback
}

func knownMsgName(t byte) string {
	switch t {
	case dhcppacket.MsgOffer:
		return "OFFER"
	case dhcppacket.MsgAck:
		return "ACK"
	case dhcppacket.MsgNak:
		return "NAK"
	case dhcppacket.MsgDecline:
		return "DECLINE"
	case dhcppacket.MsgInform:
		return "INFORM"
	default:
		return ""
	}
}

func (s *Server) identity(p *dhcppacket.Packet) storage.Identity {
	if raw := p.ClientID(); len(raw) > 0 {
		return storage.IdentityFromClientID(raw)
	}
	return storage.IdentityFromCHAddr(p.CHAddr)
}

func (s *Server) replayOutcome(rec *storage.TransactionRecord) *Outcome {
	o := &Outcome{
		Category:  storage.CatOK,
		Action:    "duplicate_replay",
		Result:    "duplicate_replay",
		Reason:    "identical_retransmission",
		Detail:    fmt.Sprintf("replaying stored %s for xid=%d fp=%s; lease timings unchanged", rec.OutType, rec.XID, rec.Fingerprint),
		Reply:     rec.Reply,
		Duplicate: true,
	}
	switch rec.OutType {
	case "OFFER":
		o.OutType = dhcppacket.MsgOffer
	case "ACK":
		o.OutType = dhcppacket.MsgAck
		if ip, err := netip.ParseAddr(rec.AssignedIP); err == nil {
			o.AssignedIP = ip
		}
	case "NAK":
		o.OutType = dhcppacket.MsgNak
		o.Category = storage.CatNAK
	}
	return o
}

func msgName(t byte) string {
	if n := knownMsgName(t); n != "" {
		return n
	}
	switch t {
	case dhcppacket.MsgDiscover:
		return "DISCOVER"
	case dhcppacket.MsgRequest:
		return "REQUEST"
	case dhcppacket.MsgRelease:
		return "RELEASE"
	default:
		return ""
	}
}

func fail(cat storage.FailureCategory, reason, detailFmt string, args ...any) *Outcome {
	return &Outcome{
		Category: cat,
		Action:   "reject",
		Result:   string(cat),
		Reason:   reason,
		Detail:   fmt.Sprintf(detailFmt, args...),
	}
}
