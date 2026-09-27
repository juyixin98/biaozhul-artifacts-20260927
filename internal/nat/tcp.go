package nat

import "natlab/internal/model"

// tcpStep is the outcome of feeding one TCP segment to the state machine.
type tcpStep struct {
	stateAfter string
	// close means the mapping must be torn down immediately (both FINs
	// acknowledged, or a delivered RST).
	close bool
	// reject means the segment is illegal in the current state and must be
	// dropped as tcp_state_conflict.
	reject         bool
	rationale      string
	finInternalSet bool
	finExternalSet bool
}

// stepTCP applies the simplified but explicit state machine. It is stateless
// apart from its arguments, which makes the transition table independently
// testable.
//
// Rules (direction is from the NAT's point of view):
//
//	SYN_SENT (internal opened the flow):
//	  outbound SYN            -> SYN_SENT (retransmit / keepalive refresh)
//	  inbound  SYN+ACK        -> ESTABLISHED
//	  outbound ACK (no SYN)   -> reject (handshake not yet answered)
//	  any FIN                 -> reject (cannot close before handshake)
//	  inbound/outbound RST    -> close (abort, port released)
//
//	ESTABLISHED:
//	  data / pure ACK either side -> ESTABLISHED (refresh)
//	  outbound FIN -> FIN_WAIT_1 (internal begins close)
//	  inbound  FIN -> FIN_WAIT_2 (external begins close, internal still open)
//	  SYN-only in either direction -> reject (no simultaneous-open simulation)
//	  RST either side -> close
//
//	FIN_WAIT_1 (internal FIN outstanding):
//	  inbound FIN (ACK optional) -> TIME_WAIT (both FINs seen)
//	  inbound ACK / outbound data -> stay (refresh)
//	  outbound FIN (retransmit)   -> stay
//	  RST -> close
//
//	FIN_WAIT_2 (internal already closed, waiting for external FIN):
//	  inbound FIN -> TIME_WAIT
//	  anything else (no RST) -> stay; only inbound traffic refreshes
//	  RST -> close
//
//	TIME_WAIT:
//	  inbound ACK (the final ACK) -> close (mapping released)
//	  duplicate FIN either side   -> stay (no refresh)
//	  anything else              -> reject; RST -> close
func stepTCP(state string, dir model.Direction, f model.TCPFlagBits) tcpStep {
	rst := f.RST

	// RST is valid against every live state and aborts at once.
	if rst {
		return tcpStep{stateAfter: StateClosed, close: true,
			rationale: "RST delivered: aborting connection and releasing external port"}
	}

	switch state {
	case StateSynSent:
		switch {
		case dir == model.Outbound && f.SYN:
			return tcpStep{stateAfter: StateSynSent,
				rationale: "SYN (retransmit) in SYN_SENT: handshake still pending, timer refreshed"}
		case dir == model.Inbound && f.SYN && f.ACK:
			return tcpStep{stateAfter: StateEstablished,
				rationale: "SYN+ACK answers the opening SYN: handshake completed"}
		case dir == model.Outbound && f.ACK && !f.SYN && !f.FIN:
			return tcpStep{stateAfter: StateSynSent, reject: true,
				rationale: "outbound ACK before any SYN+ACK: handshake never answered"}
		case f.FIN:
			return tcpStep{stateAfter: StateSynSent, reject: true,
				rationale: "FIN during SYN_SENT: cannot close an unestablished connection"}
		case dir == model.Inbound && f.SYN && !f.ACK:
			return tcpStep{stateAfter: StateSynSent, reject: true,
				rationale: "bare inbound SYN in SYN_SENT: simultaneous open not modeled"}
		default:
			return tcpStep{stateAfter: StateSynSent, reject: true,
				rationale: "segment not permitted during SYN_SENT handshake"}
		}

	case StateEstablished:
		switch {
		case f.SYN:
			return tcpStep{stateAfter: StateEstablished, reject: true,
				rationale: "SYN on an established connection: illegal retransmission"}
		case dir == model.Outbound && f.FIN:
			return tcpStep{stateAfter: StateFinWait1, finInternalSet: true,
				rationale: "internal FIN: moving to FIN_WAIT_1"}
		case dir == model.Inbound && f.FIN:
			return tcpStep{stateAfter: StateFinWait2, finExternalSet: true,
				rationale: "external FIN: moving to FIN_WAIT_2 (half-closed)"}
		default:
			return tcpStep{stateAfter: StateEstablished,
				rationale: "data/ACK in ESTABLISHED: forwarding and refreshing idle timer"}
		}

	case StateFinWait1:
		switch {
		case dir == model.Inbound && f.FIN:
			s := tcpStep{stateAfter: StateTimeWait, finExternalSet: true,
				rationale: "peer FIN received while internal FIN outstanding: moving to TIME_WAIT"}
			return s
		case dir == model.Outbound && f.FIN:
			return tcpStep{stateAfter: StateFinWait1, finInternalSet: true,
				rationale: "retransmitted internal FIN in FIN_WAIT_1"}
		case dir == model.Inbound && f.ACK:
			return tcpStep{stateAfter: StateFinWait1,
				rationale: "FIN acknowledged in FIN_WAIT_1, waiting for peer FIN"}
		default:
			return tcpStep{stateAfter: StateFinWait1,
				rationale: "segment in FIN_WAIT_1: waiting for peer FIN"}
		}

	case StateFinWait2:
		switch {
		case dir == model.Inbound && f.FIN:
			return tcpStep{stateAfter: StateTimeWait, finExternalSet: true,
				rationale: "peer FIN received in FIN_WAIT_2: moving to TIME_WAIT"}
		case dir == model.Outbound && f.FIN:
			return tcpStep{stateAfter: StateFinWait2, finInternalSet: true,
				rationale: "internal FIN while half-closed: waiting peer FIN"}
		default:
			return tcpStep{stateAfter: StateFinWait2,
				rationale: "half-closed FIN_WAIT_2: only inbound traffic refreshes"}
		}

	case StateTimeWait:
		switch {
		case dir == model.Inbound && f.ACK && !f.FIN:
			return tcpStep{stateAfter: StateClosed, close: true,
				rationale: "final ACK in TIME_WAIT: closing and releasing external port"}
		case f.FIN:
			return tcpStep{stateAfter: StateTimeWait,
				rationale: "duplicate FIN in TIME_WAIT: ignored, fixed TIME_WAIT timer kept"}
		default:
			return tcpStep{stateAfter: StateTimeWait, reject: true,
				rationale: "unexpected segment in TIME_WAIT"}
		}
	}
	return tcpStep{stateAfter: state, reject: true,
		rationale: "unknown TCP state " + state}
}
