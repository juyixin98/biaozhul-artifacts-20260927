package kernel

import (
	"time"

	"workbroker/internal/protocol"
)

// NackResult is the persistence contract of a worker-reported failure.
// Exactly one of Event/Dead is meaningful: requeue (EventTimeoutRetry-style,
// with cause worker_nack) or terminal dead-letter.
type NackResult struct {
	Message *protocol.Message
	Receipt *protocol.Receipt // marked consumed
	Failure *protocol.AttemptFailure
	Dead    *protocol.DeadReason // non-nil iff parked dead
	Event   protocol.Event
}

// NackOne records an explicit worker failure against the current delivery.
//
//   - The receipt is validated exactly like ack (an expired/stale nack is
//     rejected, not silently accepted).
//   - If the failed delivery is the MaxAttempts-th, the message is parked
//     dead with cause max_attempts_exhausted_nack and the complete history
//     (prior failures + this one).
//   - Otherwise it returns to available immediately, attempts unchanged
//     (attempts counts actual *receives*, and the next receive is what bumps
//     it).
func NackOne(m *protocol.Message, r *protocol.Receipt, newerReceiptCount int, workerReason string, now time.Time, prior []protocol.AttemptFailure) (NackResult, *protocol.Failure) {
	if f := GuardReceipt(r, m, newerReceiptCount, now); f != nil {
		f.Op = "nack"
		return NackResult{}, f
	}
	at := now.UTC()
	if workerReason == "" {
		workerReason = "worker reported failure"
	}

	failure := &protocol.AttemptFailure{
		ID:         NewID("att_"),
		MessageID:  m.ID,
		Attempt:    m.Attempts,
		ReceiptID:  r.ID,
		WorkerID:   r.WorkerID,
		Class:      protocol.CauseWorkerNack,
		Reason:     workerReason,
		HappenedAt: at,
	}
	r.Consumed = true

	history := make([]protocol.AttemptFailure, 0, len(prior)+1)
	history = append(history, prior...)
	history = append(history, *failure)

	res := NackResult{Message: m, Receipt: r, Failure: failure}

	if m.Attempts >= m.MaxAttempts {
		m.Status = protocol.StatusDead
		m.InFlightAt = time.Time{}
		m.VisibilityDeadline = time.Time{}
		m.ReceiptID = ""
		m.WorkerID = ""
		m.UpdatedAt = at
		dead := &protocol.DeadReason{
			Class:      protocol.CauseMaxAttemptsNack,
			Reason:     "max attempts exhausted after explicit worker failure: " + workerReason,
			HappenedAt: at,
			Failures:   history,
		}
		res.Dead = dead
		ev := event(protocol.EventDead, m, at)
		ev.Attempt = m.Attempts
		ev.ReceiptID = r.ID
		ev.WorkerID = r.WorkerID
		ev.Class = protocol.CauseMaxAttemptsNack
		ev.Reason = dead.Reason
		ev.Data = mustJSON(DeadData{
			Cause:   string(dead.Class),
			Reason:  dead.Reason,
			History: entries(history),
		})
		res.Event = ev
		return res, nil
	}

	m.Status = protocol.StatusAvailable
	m.AvailableAt = at
	m.InFlightAt = time.Time{}
	m.VisibilityDeadline = time.Time{}
	m.ReceiptID = ""
	m.WorkerID = ""
	m.UpdatedAt = at

	ev := event(protocol.EventNackRequeued, m, at)
	ev.Attempt = failure.Attempt
	ev.ReceiptID = r.ID
	ev.WorkerID = r.WorkerID
	ev.Class = protocol.CauseWorkerNack
	ev.Reason = failure.Reason
	ev.Data = mustJSON(RequeueData{
		Attempt:     failure.Attempt,
		WorkerID:    r.WorkerID,
		Cause:       string(protocol.CauseWorkerNack),
		FailureID:   failure.ID,
		AvailableAt: rfc3339(at),
	})
	res.Event = ev
	return res, nil
}
