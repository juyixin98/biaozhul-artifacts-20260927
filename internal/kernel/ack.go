package kernel

import (
	"time"

	"workbroker/internal/protocol"
)

// AckResult is the persistence contract of a successful confirmation.
type AckResult struct {
	Message *protocol.Message
	Receipt *protocol.Receipt // marked consumed
	Event   protocol.Event
}

// AckOne confirms the current delivery. On success the message transitions to
// the terminal acked state and the receipt is marked consumed. Any invalid
// receipt (unknown / expired / stale / against a dead or already acked
// message) is rejected with a concrete FailureClass and mutates nothing.
func AckOne(m *protocol.Message, r *protocol.Receipt, newerReceiptCount int, now time.Time) (AckResult, *protocol.Failure) {
	if f := GuardReceipt(r, m, newerReceiptCount, now); f != nil {
		f.Op = "ack"
		return AckResult{}, f
	}
	at := now.UTC()
	r.Consumed = true

	attempt := m.Attempts
	worker := r.WorkerID
	m.Status = protocol.StatusAcked
	m.InFlightAt = time.Time{}
	m.VisibilityDeadline = time.Time{}
	m.ReceiptID = ""
	m.WorkerID = ""
	m.UpdatedAt = at

	ev := event(protocol.EventAcked, m, at)
	ev.Attempt = attempt
	ev.ReceiptID = r.ID
	ev.WorkerID = worker
	ev.Data = mustJSON(AckData{Attempt: attempt, WorkerID: worker})
	return AckResult{Message: m, Receipt: r, Event: ev}, nil
}
