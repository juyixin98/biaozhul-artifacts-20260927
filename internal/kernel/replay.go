package kernel

import (
	"fmt"
	"time"

	"localbroker/internal/protocol"
)

// ReplayedMessage is the state reconstructed solely from the event log. It is
// intentionally separate from protocol.Message: replay must never read live
// table state, so it cannot silently agree with the store under test.
type ReplayedMessage struct {
	ID          string
	State       protocol.State
	Attempts    int
	Receipt     string
	ReceiptGen  int64
	LastReceipt string
	Deadline    time.Time
	Failures    []protocol.Failure
	EnqueuedSeq int64
	LastSeq     int64
}

// Apply folds one event into a replayed state. It returns an error (never a
// silent success) for unknown event types, unknown target states or events
// applied before enqueue, so corrupt/partial logs are surfaced.
func Apply(rm *ReplayedMessage, ev Event) error {
	if rm == nil {
		return fmt.Errorf("%w: nil state", ErrUnknownState)
	}
	if ev.Type != EvEnqueued && rm.LastSeq == 0 && rm.State == "" {
		return fmt.Errorf("event %s for %s before enqueue", ev.Type, ev.MessageID)
	}
	switch ev.Type {
	case EvEnqueued:
		if rm.State != "" {
			return fmt.Errorf("duplicate enqueue for %s at seq %d", ev.MessageID, ev.Seq)
		}
		rm.ID = ev.MessageID
		rm.State = protocol.StateAvailable
		rm.EnqueuedSeq = ev.Seq
	case EvClaimed:
		if rm.State != protocol.StateAvailable {
			return fmt.Errorf("claimed from state %q at seq %d", rm.State, ev.Seq)
		}
		rm.State = protocol.StateInvisible
		rm.Attempts = ev.Attempts
		rm.LastReceipt = rm.Receipt
		rm.Receipt = ev.Receipt
		rm.ReceiptGen = ev.ReceiptGen
		rm.Deadline = ev.Deadline
	case EvExtended:
		if rm.State != protocol.StateInvisible {
			return fmt.Errorf("extended in state %q at seq %d", rm.State, ev.Seq)
		}
		rm.Deadline = ev.Deadline
		rm.Receipt = ev.Receipt
	case EvLeaseExpired:
		if rm.State != protocol.StateInvisible {
			return fmt.Errorf("lease expired in state %q at seq %d", rm.State, ev.Seq)
		}
		rm.toAvailable(ev)
	case EvNacked:
		if rm.State != protocol.StateInvisible {
			return fmt.Errorf("nacked in state %q at seq %d", rm.State, ev.Seq)
		}
		rm.toAvailable(ev)
	case EvDeadLettered:
		if rm.State != protocol.StateInvisible {
			return fmt.Errorf("dead-lettered from state %q at seq %d", rm.State, ev.Seq)
		}
		if ev.Failure != nil {
			rm.Failures = append(rm.Failures, *ev.Failure)
		}
		rm.State = protocol.StateDead
		rm.Receipt = ""
		rm.Deadline = time.Time{}
	case EvAcked:
		if rm.State != protocol.StateInvisible {
			return fmt.Errorf("acked from state %q at seq %d", rm.State, ev.Seq)
		}
		rm.State = protocol.StateAcked
		rm.Receipt = ""
		rm.Deadline = time.Time{}
	default:
		return fmt.Errorf("%w: event type %q", ErrUnknownState, ev.Type)
	}
	rm.LastSeq = ev.Seq
	return nil
}

// toAvailable handles the lease_expired/nacked transitions: append the failure
// reason and release the lease while remembering the last receipt.
func (rm *ReplayedMessage) toAvailable(ev Event) {
	if ev.Failure != nil {
		rm.Failures = append(rm.Failures, *ev.Failure)
	}
	rm.State = protocol.StateAvailable
	rm.LastReceipt = rm.Receipt
	rm.Receipt = ev.Receipt // kernels clear the live receipt ("" on expire/nack)
	rm.Deadline = time.Time{}
}

// Replay folds an ordered event stream per message into replayed states. The
// returned map is keyed by message id. Any structural inconsistency in the log
// stops replay and is returned — partial state is never presented as truth.
func Replay(events []Event) (map[string]*ReplayedMessage, error) {
	out := make(map[string]*ReplayedMessage)
	for _, ev := range events {
		rm := out[ev.MessageID]
		if rm == nil {
			rm = &ReplayedMessage{}
			out[ev.MessageID] = rm
		}
		if err := Apply(rm, ev); err != nil {
			return nil, fmt.Errorf("replay seq=%d message=%s: %w", ev.Seq, ev.MessageID, err)
		}
	}
	return out, nil
}
