package kernel

import (
	"time"

	"workbroker/internal/protocol"
)

// ReceiveResult is what the store persists atomically when handing a visible
// message to a worker: the bumped message, the freshly minted receipt and the
// receive event.
type ReceiveResult struct {
	Message *protocol.Message
	Receipt *protocol.Receipt
	Event   protocol.Event
}

// ReceiveOne performs one receive against an eligible message (status
// available, AvailableAt <= now). Each receive is an *actual delivery*, so
// Attempts increments by one and a brand-new receipt is minted. An earlier
// receipt never remains valid after this — it is the store's responsibility to
// have marked timed-out receipts consumed during reap, and the message only
// references the new receipt.
//
// visibility is clamped to [MinVisibility, MaxVisibility].
func ReceiveOne(m *protocol.Message, workerID string, visibility time.Duration, now time.Time) ReceiveResult {
	if m.Status != protocol.StatusAvailable {
		panic("kernel: ReceiveOne on non-available message " + m.ID)
	}
	at := now.UTC()
	visibility = clampVisibility(visibility)
	deadline := at.Add(visibility)

	attempt := m.Attempts + 1
	rcp := &protocol.Receipt{
		ID:        NewID("rcp_"),
		MessageID: m.ID,
		WorkerID:  workerID,
		IssuedAt:  at,
		ExpiresAt: deadline,
	}

	m.Status = protocol.StatusInFlight
	m.Attempts = attempt
	m.InFlightAt = at
	m.VisibilityDeadline = deadline
	m.ReceiptID = rcp.ID
	m.WorkerID = workerID
	m.UpdatedAt = at

	ev := event(protocol.EventReceived, m, at)
	ev.Attempt = attempt
	ev.ReceiptID = rcp.ID
	ev.WorkerID = workerID
	ev.Data = mustJSON(ReceiveData{
		Attempt:    attempt,
		WorkerID:   workerID,
		ExpiresAt:  rfc3339(deadline),
		FromStatus: string(protocol.StatusAvailable),
	})
	return ReceiveResult{Message: m, Receipt: rcp, Event: ev}
}
