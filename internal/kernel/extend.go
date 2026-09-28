package kernel

import (
	"time"

	"workbroker/internal/protocol"
)

// ExtendResult is the persistence contract of a successful
// ChangeMessageVisibility: bumped receipt deadline/message deadline and the
// visibility_extended event.
type ExtendResult struct {
	Message *protocol.Message
	Receipt *protocol.Receipt
	Event   protocol.Event
}

// ExtendOne runs the guard and, if the receipt is still the live current
// delivery, extends the visibility.
//
// Extension is relative to *now* (SQS ChangeMessageVisibility semantics): the
// new deadline is max(existing deadline, now+extend). Taking the max means a
// duplicate or back-to-back extend can never *shorten* an already-longer
// window, which is what makes "repeated extend" well-defined and monotonic
// even under retries. extend is clamped to [MinVisibility, MaxVisibility].
//
// Caller must have validated the receipt via the same atomic section used to
// persist the result (the stores do).
func ExtendOne(m *protocol.Message, r *protocol.Receipt, newerReceiptCount int, extend time.Duration, now time.Time) (ExtendResult, *protocol.Failure) {
	if f := GuardReceipt(r, m, newerReceiptCount, now); f != nil {
		f.Op = "extend"
		return ExtendResult{}, f
	}
	at := now.UTC()
	extend = clampVisibility(extend)
	old := r.ExpiresAt
	cand := at.Add(extend)
	if !cand.After(old) {
		// Duplicate/back-to-back extend landing inside the previous window:
		// keep the longer deadline but still record the request so the event
		// log shows every attempt.
		cand = old
	}
	r.ExpiresAt = cand
	if r.ExtendedAt.IsZero() {
		r.ExtendedAt = at
	}
	m.VisibilityDeadline = cand
	m.UpdatedAt = at

	ev := event(protocol.EventVisibilityExt, m, at)
	ev.Attempt = m.Attempts
	ev.ReceiptID = r.ID
	ev.WorkerID = r.WorkerID
	ev.Data = mustJSON(ExtendData{
		WorkerID:      r.WorkerID,
		OldExpiresAt:  rfc3339(old),
		NewExpiresAt:  rfc3339(cand),
		ExtendSeconds: int64(extend.Seconds()),
	})
	return ExtendResult{Message: m, Receipt: r, Event: ev}, nil
}
