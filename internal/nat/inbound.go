package nat

import (
	"context"
	"strconv"
	"time"

	"natlab/internal/model"
)

func portStr(p uint16) string { return strconv.FormatUint(uint64(p), 10) }

// evaluateInbound handles public -> private packets. It enforces the
// endpoint-dependent filter and the TCP state machine. Expired mappings were
// already swept by the caller, so a missing active mapping is either a late
// return (history row exists) or traffic to an unowned external port.
func (e *Engine) evaluateInbound(ctx context.Context, rs *runState, runID string, p model.Packet, now time.Time) (model.Decision, error) {
	ext := p.FiveTuple.DstPort

	m, err := e.store.ActiveByExtPort(ctx, runID, p.FiveTuple.Protocol, ext, now)
	if err != nil {
		return model.Decision{}, computeErr(err)
	}
	if m == nil {
		// Distinguish "return to an expired mapping" from "never owned".
		hist, err := e.store.HistoryByExtPort(ctx, runID, p.FiveTuple.Protocol, ext)
		if err != nil {
			return model.Decision{}, computeErr(err)
		}
		if hist != nil {
			return reject(model.CatStateConflict, model.CodeLateReturnExpired,
				"return packet arrived after mapping expiry; port may already be reused", p.ObservedAt, now), nil
		}
		return reject(model.CatStateConflict, model.CodeInboundNoMapping,
			"no active mapping owns external port", p.ObservedAt, now), nil
	}

	// Endpoint-dependent filter: source must be the exact remote endpoint.
	if p.FiveTuple.SrcIP != m.DstIP || p.FiveTuple.SrcPort != m.DstPort {
		return reject(model.CatStateConflict, model.CodeEndpointFiltered,
			"endpoint-dependent filter: remote "+p.FiveTuple.SrcIP+":"+portStr(p.FiveTuple.SrcPort)+
				" != mapped peer "+m.DstIP+":"+portStr(m.DstPort), p.ObservedAt, now), nil
	}

	if p.FiveTuple.Protocol == model.TCP {
		f, badReason := parseTCPFlags(p.Flags)
		if badReason != "" {
			return reject(model.CatInvalidInput, model.CodeBadFlag, badReason, p.ObservedAt, now), nil
		}
		if f.RST {
			return e.closeOnRST(ctx, rs, runID, p, m, now, false)
		}
		next, closeNow, code, reason := fsmInbound(m.State, f)
		if code != "" {
			return reject(model.CatStateConflict, code, reason, p.ObservedAt, now), nil
		}
		m.State = next
		e.touchMapping(m, now)
		if err := e.store.UpdateMapping(ctx, runID, m); err != nil {
			return model.Decision{}, computeErr(err)
		}
		if closeNow {
			return e.finishClose(ctx, rs, runID, p, m, now, false)
		}
		return e.acceptIn(p, m, now), nil
	}

	// UDP: matching endpoint refreshes and is forwarded.
	e.touchMapping(m, now)
	if err := e.store.UpdateMapping(ctx, runID, m); err != nil {
		return model.Decision{}, computeErr(err)
	}
	return e.acceptIn(p, m, now), nil
}

func (e *Engine) acceptIn(p model.Packet, m *model.Mapping, now time.Time) model.Decision {
	return model.Decision{
		Accepted: true, ObservedAt: p.ObservedAt, EffectiveAt: now,
		Mapping: m,
		Translated: &model.FiveTuple{
			SrcIP: m.DstIP, SrcPort: m.DstPort,
			DstIP: m.SrcIP, DstPort: m.SrcPort,
			Protocol: p.FiveTuple.Protocol,
		},
	}
}

// closeOnRST handles an RST matching the peer endpoint: the mapping closes
// immediately and, when configured, frees the external port.
func (e *Engine) closeOnRST(ctx context.Context, rs *runState, runID string, p model.Packet, m *model.Mapping, now time.Time, outbound bool) (model.Decision, error) {
	if !e.cfg.CloseFreesPort {
		m.State = model.StateClosed
		e.touchMapping(m, now)
		if err := e.store.UpdateMapping(ctx, runID, m); err != nil {
			return model.Decision{}, computeErr(err)
		}
		return e.acceptWith(p, m, now, outbound), nil
	}
	return e.finishClose(ctx, rs, runID, p, m, now, outbound)
}

// finishClose marks the mapping closed in the store and releases its port.
func (e *Engine) finishClose(ctx context.Context, rs *runState, runID string, p model.Packet, m *model.Mapping, now time.Time, outbound bool) (model.Decision, error) {
	if _, err := e.store.CloseMapping(ctx, runID, m.ID, now); err != nil {
		return model.Decision{}, computeErr(err)
	}
	m.State = model.StateClosed
	rs.pool.Release(m.MappedPort)
	return e.acceptWith(p, m, now, outbound), nil
}

func (e *Engine) acceptWith(p model.Packet, m *model.Mapping, now time.Time, outbound bool) model.Decision {
	if outbound {
		return e.acceptOut(p, m, now)
	}
	return e.acceptIn(p, m, now)
}

// fsmInbound mirrors fsmOutbound for remote -> local TCP segments.
func fsmInbound(state string, f tcpFlags) (next string, closeNow bool, code, reason string) {
	switch state {
	case model.StateSynSent:
		if f.SYN && f.ACK {
			return model.StateSynAckRcvd, false, "", ""
		}
		if f.SYN {
			// A bare SYN reply is not a valid handshake answer.
			return "", false, model.CodeTCPBadState, "inbound SYN (without ACK) in syn_sent is not a handshake reply"
		}
		return "", false, model.CodeTCPBadState, "inbound packet does not fit syn_sent"
	case model.StateSynAckRcvd:
		// Retransmitted SYN-ACK is accepted idempotently.
		if f.SYN && f.ACK {
			return model.StateSynAckRcvd, false, "", ""
		}
		return "", false, model.CodeTCPBadState, "inbound packet does not fit syn_ack_rcvd"
	case model.StateEstablished:
		if f.FIN && f.ACK {
			return model.StateFinWait, false, "", ""
		}
		return model.StateEstablished, false, "", ""
	case model.StateFinWait:
		if f.FIN {
			// Simultaneous close: peer also closed.
			return model.StateFinWait, false, "", ""
		}
		if f.ACK {
			// ACK of our FIN: exchange complete.
			return model.StateClosed, true, "", ""
		}
		return "", false, model.CodeTCPBadState, "inbound packet does not fit fin_wait"
	default:
		return "", false, model.CodeTCPBadState, "no inbound transition from " + state
	}
}
