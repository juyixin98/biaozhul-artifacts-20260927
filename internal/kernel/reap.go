// Package kernel is the compute core of the broker. It is deliberately free
// of SQL, HTTP and synchronization: every function takes domain values plus
// "now" and returns the exact mutations + event(s) to persist.
//
// Both storage backends (in-memory and PostgreSQL) call the same functions
// under their own atomicity boundary (a mutex / a row-locking transaction),
// so the visibility-timeout race has identical semantics regardless of
// backend. Tests treat the hand-derived golden outcomes in kernel_test.go as
// the reference answers; the stores are checked against that same behavior.
package kernel

import (
	"time"

	"workbroker/internal/protocol"
	"workbroker/internal/version"
)

// Limits enforced by the compute core. The HTTP/config layers share these.
const (
	// MinVisibility clamps visibility/extend durations.
	MinVisibility = time.Second
	// MaxVisibility is the upper bound (SQS-style).
	MaxVisibility = 12 * time.Hour
	// DefaultVisibility is used when a receive omits the duration.
	DefaultVisibility = 30 * time.Second
	// DefaultMaxAttempts is the default delivery ceiling.
	DefaultMaxAttempts int64 = 3
)

// clampVisibility bounds d into [MinVisibility, MaxVisibility].
func clampVisibility(d time.Duration) time.Duration {
	if d < MinVisibility {
		return MinVisibility
	}
	if d > MaxVisibility {
		return MaxVisibility
	}
	return d
}

func rfc3339(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }

func event(tp protocol.EventType, m *protocol.Message, at time.Time) protocol.Event {
	return protocol.Event{
		Type:      tp,
		MessageID: m.ID,
		Partition: m.Partition,
		At:        at.UTC(),
		Version:   version.ProtocolVersion,
	}
}

// ---------------------------------------------------------------------------
// Visibility timeout: ReapOne
// ---------------------------------------------------------------------------

// ReapResult tells the store exactly what to persist for one expired inflight
// message: either a requeue (retry) or a permanent dead-letter transition.
type ReapResult struct {
	Message *protocol.Message // mutated message
	Receipt *protocol.Receipt // the consumed (expired) receipt
	Failure *protocol.AttemptFailure
	Dead    *protocol.DeadReason // non-nil iff parked dead
	Event   protocol.Event
	// DeleteFailure is true only when the attempt failure row is part of the
	// dead history and should not be inserted separately (it is always false:
	// dead payload embeds the same failure, which the store still inserts as a
	// row; kept for explicitness of the persistence contract).
}

// ReapOne advances one message whose visibility deadline has passed
// (now >= deadline) and whose receipt has not been consumed.
//
// at is the moment of the transition. The function mutates m/r in place and
// returns what the store must persist atomically with it.
//
// Attempts equals the number of actual receives. When the attempt that just
// timed out was the MaxAttempts-th, the message is parked dead with the full
// failure history (prior failures passed in + this one). Otherwise it returns
// to available immediately and can be received again.
func ReapOne(m *protocol.Message, r *protocol.Receipt, now time.Time, prior []protocol.AttemptFailure) ReapResult {
	if m.Status != protocol.StatusInFlight {
		panic("kernel: ReapOne on non-inflight message " + m.ID)
	}
	if r == nil || r.Consumed {
		panic("kernel: ReapOne without an unconsumed receipt for " + m.ID)
	}

	at := now.UTC()
	failure := &protocol.AttemptFailure{
		ID:         NewID("att_"),
		MessageID:  m.ID,
		Attempt:    m.Attempts,
		ReceiptID:  r.ID,
		WorkerID:   r.WorkerID,
		Class:      protocol.CauseVisibilityTimeout,
		Reason:     "visibility deadline elapsed without ack",
		HappenedAt: at,
	}
	// The receipt is spent: redelivery must issue a new one.
	r.Consumed = true

	history := make([]protocol.AttemptFailure, 0, len(prior)+1)
	history = append(history, prior...)
	history = append(history, *failure)

	res := ReapResult{Message: m, Receipt: r, Failure: failure}

	if m.Attempts >= m.MaxAttempts {
		// Terminal: dead-letter with complete history.
		m.Status = protocol.StatusDead
		m.InFlightAt = time.Time{}
		m.VisibilityDeadline = time.Time{}
		m.ReceiptID = ""
		m.WorkerID = ""
		m.UpdatedAt = at
		dead := &protocol.DeadReason{
			Class:      protocol.CauseMaxAttempts,
			Reason:     "max attempts exhausted after visibility timeout",
			HappenedAt: at,
			Failures:   history,
		}
		res.Dead = dead
		ev := event(protocol.EventDead, m, at)
		ev.Attempt = m.Attempts
		ev.ReceiptID = r.ID
		ev.WorkerID = r.WorkerID
		ev.Class = protocol.CauseMaxAttempts
		ev.Reason = dead.Reason
		ev.Data = mustJSON(DeadData{
			Cause:   string(dead.Class),
			Reason:  dead.Reason,
			History: entries(history),
		})
		res.Event = ev
		return res
	}

	// Retry: visible again immediately.
	m.Status = protocol.StatusAvailable
	m.AvailableAt = at
	m.InFlightAt = time.Time{}
	m.VisibilityDeadline = time.Time{}
	m.ReceiptID = ""
	m.WorkerID = ""
	m.UpdatedAt = at

	ev := event(protocol.EventTimeoutRetry, m, at)
	ev.Attempt = failure.Attempt
	ev.ReceiptID = r.ID
	ev.WorkerID = r.WorkerID
	ev.Class = protocol.CauseVisibilityTimeout
	ev.Reason = failure.Reason
	ev.Data = mustJSON(RequeueData{
		Attempt:     failure.Attempt,
		WorkerID:    r.WorkerID,
		Cause:       string(protocol.CauseVisibilityTimeout),
		FailureID:   failure.ID,
		AvailableAt: rfc3339(at),
	})
	res.Event = ev
	return res
}

func entries(history []protocol.AttemptFailure) []FailureEntry {
	out := make([]FailureEntry, len(history))
	for i, f := range history {
		out[i] = FailureEntry{
			FailureID:  f.ID,
			Attempt:    f.Attempt,
			ReceiptID:  f.ReceiptID,
			WorkerID:   f.WorkerID,
			Cause:      string(f.Class),
			Reason:     f.Reason,
			HappenedAt: rfc3339(f.HappenedAt),
		}
	}
	return out
}
