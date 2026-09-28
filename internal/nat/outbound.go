package nat

import (
	"context"
	"time"

	"natlab/internal/model"
)

// evaluateOutbound handles private -> public packets. Caller holds the engine
// lock; expiry sweep already happened. A non-nil error is a compute failure.
func (e *Engine) evaluateOutbound(ctx context.Context, rs *runState, runID string, p model.Packet, now time.Time) (model.Decision, error) {
	key := model.FlowKey{
		Protocol: p.FiveTuple.Protocol,
		SrcIP:    p.FiveTuple.SrcIP, SrcPort: p.FiveTuple.SrcPort,
		DstIP: p.FiveTuple.DstIP, DstPort: p.FiveTuple.DstPort,
	}
	m, err := e.store.ActiveByFlow(ctx, runID, key, now)
	if err != nil {
		return model.Decision{}, computeErr(err)
	}
	if m == nil {
		return e.createOutbound(ctx, rs, runID, p, now)
	}

	// Existing mapping: refresh and (TCP only) walk the state machine.
	if p.FiveTuple.Protocol == model.TCP {
		f, badReason := parseTCPFlags(p.Flags)
		if badReason != "" { // already validated; defensive
			return reject(model.CatInvalidInput, model.CodeBadFlag, badReason, p.ObservedAt, now), nil
		}
		if f.RST {
			return e.closeOnRST(ctx, rs, runID, p, m, now, true)
		}
		next, closeNow, code, reason := fsmOutbound(m.State, f)
		if code != "" {
			return reject(model.CatStateConflict, code, reason, p.ObservedAt, now), nil
		}
		m.State = next
		e.touchMapping(m, now)
		if err := e.store.UpdateMapping(ctx, runID, m); err != nil {
			return model.Decision{}, computeErr(err)
		}
		if closeNow {
			return e.finishClose(ctx, rs, runID, p, m, now, true)
		}
		return e.acceptOut(p, m, now), nil
	}

	// UDP: every datagram on the flow refreshes it.
	e.touchMapping(m, now)
	if err := e.store.UpdateMapping(ctx, runID, m); err != nil {
		return model.Decision{}, computeErr(err)
	}
	return e.acceptOut(p, m, now), nil
}

func (e *Engine) createOutbound(ctx context.Context, rs *runState, runID string, p model.Packet, now time.Time) (model.Decision, error) {
	state := model.StateOpen
	if p.FiveTuple.Protocol == model.TCP {
		f, _ := parseTCPFlags(p.Flags)
		if !f.SYN || f.ACK || f.FIN || f.RST {
			return reject(model.CatStateConflict, model.CodeTCPNonSynOutbound,
				"outbound TCP packet on unknown flow must be a bare SYN", p.ObservedAt, now), nil
		}
		state = model.StateSynSent
	}

	port, ok := rs.pool.Allocate()
	if !ok {
		return reject(model.CatResourceExhausted, model.CodePortExhausted,
			"external port pool exhausted", p.ObservedAt, now), nil
	}
	ttl, okTTL := e.cfg.TTL(p.FiveTuple.Protocol, state)
	if !okTTL {
		rs.pool.Release(port)
		return reject(model.CatComputeFailure, model.CodeStoreError,
			"no TTL configured for state "+state, p.ObservedAt, now), nil
	}
	m := &model.Mapping{
		RunID: runID, Protocol: p.FiveTuple.Protocol,
		SrcIP: p.FiveTuple.SrcIP, SrcPort: p.FiveTuple.SrcPort,
		DstIP: p.FiveTuple.DstIP, DstPort: p.FiveTuple.DstPort,
		MappedPort: port, State: state,
		CreatedAt: now, LastUsedAt: now, ExpiresAt: now.Add(ttl),
	}
	id, err := e.store.InsertMapping(ctx, runID, m)
	if err != nil {
		rs.pool.Release(port)
		return model.Decision{}, computeErr(err)
	}
	m.ID = id
	return e.acceptOut(p, m, now), nil
}

// touchMapping refreshes last-used/expiry with the TTL of the current state.
func (e *Engine) touchMapping(m *model.Mapping, now time.Time) {
	ttl, ok := e.cfg.TTL(m.Protocol, m.State)
	if !ok {
		return
	}
	m.LastUsedAt = now
	m.ExpiresAt = now.Add(ttl)
}

func (e *Engine) acceptOut(p model.Packet, m *model.Mapping, now time.Time) model.Decision {
	return model.Decision{
		Accepted: true, ObservedAt: p.ObservedAt, EffectiveAt: now,
		Mapping: m,
		Translated: &model.FiveTuple{
			SrcIP: e.cfg.PublicIPString(), SrcPort: m.MappedPort,
			DstIP: p.FiveTuple.DstIP, DstPort: p.FiveTuple.DstPort,
			Protocol: p.FiveTuple.Protocol,
		},
	}
}

// fsmOutbound returns the next state, whether the exchange is now complete
// (both FINs seen), or a rejection code.
func fsmOutbound(state string, f tcpFlags) (next string, closeNow bool, code, reason string) {
	switch state {
	case model.StateSynSent:
		// Retransmitted SYN is benign; ACK before SYN-ACK is bogus.
		if f.SYN && !f.ACK {
			return model.StateSynSent, false, "", ""
		}
		if f.ACK && !f.SYN && !f.FIN && !f.RST {
			return "", false, model.CodeTCPBadState, "outbound ACK in syn_sent before SYN-ACK was received"
		}
		if f.RST {
			return state, false, "", ""
		}
		if f.FIN {
			return "", false, model.CodeTCPBadState, "outbound FIN in syn_sent before connection established"
		}
		return "", false, model.CodeTCPBadState, "outbound packet does not fit syn_sent"
	case model.StateSynAckRcvd:
		if f.ACK && !f.SYN && !f.FIN && !f.RST {
			return model.StateEstablished, false, "", ""
		}
		if f.SYN {
			return "", false, model.CodeTCPBadState, "unexpected outbound SYN in syn_ack_rcvd"
		}
		if f.FIN {
			return "", false, model.CodeTCPBadState, "outbound FIN before handshake completed"
		}
		if f.RST {
			return state, false, "", ""
		}
		return "", false, model.CodeTCPBadState, "outbound packet does not fit syn_ack_rcvd"
	case model.StateEstablished:
		if f.FIN && f.ACK {
			return model.StateFinWait, false, "", ""
		}
		if f.RST {
			return state, false, "", ""
		}
		// Data ACKs and keep-alives keep the mapping established and fresh.
		return model.StateEstablished, false, "", ""
	case model.StateFinWait:
		if f.ACK && !f.FIN {
			// Final ACK after the peer FIN: the exchange is complete.
			return model.StateClosed, true, "", ""
		}
		if f.FIN {
			// Simultaneous close: stay until the closing ACK arrives.
			return model.StateFinWait, false, "", ""
		}
		if f.RST {
			return state, false, "", ""
		}
		return "", false, model.CodeTCPBadState, "outbound packet does not fit fin_wait"
	default:
		return "", false, model.CodeTCPBadState, "no outbound transition from " + state
	}
}
