package kernel

import (
	"fmt"
	"time"

	"workbroker/internal/protocol"
)

// GuardReceipt decides whether a receipt presented to ack/nack/extend may act
// on its message. It returns a *protocol.Failure with a concrete class for
// every rejection so callers never have to guess:
//
//   - receipt nil                         -> receipt_not_found
//   - message missing (shouldn't happen)  -> message_not_found / internal
//   - message dead                        -> message_dead
//   - message acked                       -> receipt_stale
//   - receipt belongs to an older delivery:
//     consumed receipt with a newer receipt issued (redelivery happened,
//     or message held under a newer current delivery)
//     -> receipt_stale
//     unconsumed but deadline passed    -> receipt_expired
//     unconsumed, current, live         -> (nil — allowed)
//
// "newer receipts issued" is passed in as newerReceiptCount: number of
// receipts for this message whose IssuedAt is strictly after r.IssuedAt. The
// store computes it; the kernel defines the decision. This split matters
// because an expired receipt is "late against the same delivery", while a
// stale receipt is "late against a *later* delivery" — the requirement that
// an old receipt cannot confirm a redelivered message maps to receipt_stale.
func GuardReceipt(r *protocol.Receipt, m *protocol.Message, newerReceiptCount int, now time.Time) *protocol.Failure {
	if r == nil {
		return protocol.NewFailure("guard", protocol.FailReceiptNotFound,
			"receipt does not exist", nil)
	}
	if m == nil {
		return protocol.NewFailure("guard", protocol.FailMessageNotFound,
			fmt.Sprintf("message %q referenced by receipt %q is gone", r.MessageID, r.ID), nil)
	}
	switch m.Status {
	case protocol.StatusDead:
		return protocol.NewFailure("guard", protocol.FailMessageDead,
			fmt.Sprintf("message %q is in the dead-letter area", m.ID), nil)
	case protocol.StatusAcked:
		return protocol.NewFailure("guard", protocol.FailReceiptStale,
			fmt.Sprintf("message %q was already confirmed; receipt %q is late", m.ID, r.ID), nil)
	case protocol.StatusAvailable:
		// Message is visible again. Decide *why* the receipt is late.
		if r.Consumed || newerReceiptCount > 0 {
			return protocol.NewFailure("guard", protocol.FailReceiptStale,
				fmt.Sprintf("receipt %q belongs to an earlier delivery of message %q (already redelivered)",
					r.ID, m.ID), nil)
		}
		// Not consumed and no newer receipt: the visibility window elapsed
		// but nobody has re-received it yet.
		return protocol.NewFailure("guard", protocol.FailReceiptExpired,
			fmt.Sprintf("receipt %q expired at %s; message %q became visible before redelivery",
				r.ID, r.ExpiresAt.Format("2006-01-02T15:04:05.999999999Z07:00"), m.ID), nil)
	case protocol.StatusInFlight:
		if m.ReceiptID != r.ID || newerReceiptCount > 0 {
			// The current delivery uses a different (newer) receipt — this
			// receipt belongs to an earlier delivery of the same message.
			return protocol.NewFailure("guard", protocol.FailReceiptStale,
				fmt.Sprintf("receipt %q is not the current receipt for inflight message %q", r.ID, m.ID), nil)
		}
		if r.Consumed {
			return protocol.NewFailure("guard", protocol.FailReceiptStale,
				fmt.Sprintf("receipt %q for message %q was already consumed", r.ID, m.ID), nil)
		}
		if now.UTC().After(r.ExpiresAt) || now.UTC().Equal(r.ExpiresAt) {
			// Deadline boundary: now == expires means the visibility has
			// elapsed. Because receive/extend/ack run in one atomic section
			// against reap, a result here is unambiguous.
			return protocol.NewFailure("guard", protocol.FailReceiptExpired,
				fmt.Sprintf("receipt %q expired at %s",
					r.ID, r.ExpiresAt.Format("2006-01-02T15:04:05.999999999Z07:00")), nil)
		}
		return nil
	default:
		// Unknown status: never pretend success.
		return protocol.NewFailure("guard", protocol.FailInternal,
			fmt.Sprintf("message %q in unknown status %q", m.ID, m.Status), nil)
	}
}
