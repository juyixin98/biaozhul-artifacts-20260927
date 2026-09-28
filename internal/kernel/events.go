package kernel

import (
	"time"

	"localbroker/internal/protocol"
)

// EventType enumerates the append-only state transitions.
type EventType string

const (
	EvEnqueued      EventType = "enqueued"
	EvClaimed       EventType = "claimed"
	EvExtended      EventType = "extended"
	EvAcked         EventType = "acked"
	EvLeaseExpired  EventType = "lease_expired"
	EvNacked        EventType = "nacked"
	EvDeadLettered  EventType = "dead_lettered"
)

// Event is one immutable state transition. Seq is store-assigned and monotonic
// per queue; Version identifies the kernel that produced it; RunID is filled by
// the service layer (the kernel itself is run-agnostic) and ties the row to a
// concrete test/process run so failures in logs are correlatable.
type Event struct {
	Seq        int64
	MessageID  string
	Type       EventType
	Version    string
	RunID      string
	At         time.Time
	// Transition payload (only populated fields are meaningful per Type).
	ToState       protocol.State
	Attempts      int
	Receipt       string
	ReceiptGen    int64
	Deadline      time.Time
	VisibilityTO  time.Duration
	Extra         time.Duration // extend amount
	Failure       *protocol.Failure
}

// NewEnqueued builds the initial event for a published message. The store
// assigns Seq; RunID is attached by the service layer.
func NewEnqueued(id string, now time.Time) Event {
	return Event{
		MessageID: id,
		Type:      EvEnqueued,
		Version:   protocol.Version,
		At:        now,
		ToState:   protocol.StateAvailable,
	}
}

// Claimed reports the delivery granted by a Claim: the leased message plus the
// fresh receipt. ReceiptGen is also returned so callers can prove that a
// redelivery produced a different receipt generation.
type Claimed struct {
	MessageID  string
	Attempts   int
	Receipt    string
	ReceiptGen int64
	Deadline   time.Time
}

// DecideClaim is the pure claim rule applied to a lockable available row.
//
// Attempts is the count of claims already made. MaxAttempts counts actual
// claims/deliveries: if a message has already exhausted MaxAttempts deliveries
// (e.g. all leases lapsed while no claim row was selectable yet), the next
// delivery must not be granted and the message is dead-lettered instead.
func DecideClaim(attempts, maxAttempts int) bool {
	return attempts < maxAttempts
}

// NewClaimed builds the claimed event for message id. It increments both the
// attempt count (an actual delivery happened) and the receipt generation (the
// previous receipt, if any, is now stale and must not ack this delivery).
func NewClaimed(id string, prev protocol.LeaseView, visibilityTO time.Duration, now time.Time, receipt string) (Event, Claimed) {
	attempts := prev.Attempts + 1
	gen := prev.ReceiptGen + 1
	ev := Event{
		MessageID: id,
		Type:      EvClaimed,
		Version:   protocol.Version,
		At:        now,
		ToState:   protocol.StateInvisible,
		Attempts:  attempts,
		Receipt:   receipt,
		ReceiptGen: gen,
		Deadline:  now.Add(visibilityTO),
		VisibilityTO: visibilityTO,
	}
	return ev, Claimed{
		MessageID:  id,
		Attempts:   attempts,
		Receipt:    receipt,
		ReceiptGen: gen,
		Deadline:   ev.Deadline,
	}
}

// CheckReceipt validates an ack/extend/nack receipt against the current lease.
// It encodes two distinct failure categories required by the spec:
//
//   - invisible + wrong receipt  -> ErrStaleReceipt (an old receipt cannot act
//     on a delivery that was re-granted to someone else);
//   - invisible + right receipt but now >= Deadline -> ErrLeaseExpired
//     (visibility timeout won the race; extend vs timeout is decided atomically
//     by the caller under a row lock, so "at deadline" means timeout wins).
//
// Terminal/non-leased states return their precise category rather than success.
func CheckReceipt(cur protocol.LeaseView, receipt string, now time.Time) error {
	if receipt == "" {
		return ErrInvalidReceipt
	}
	switch cur.State {
	case protocol.StateInvisible:
		if receipt != cur.Receipt {
			return ErrStaleReceipt
		}
		if !now.Before(cur.Deadline) {
			return ErrLeaseExpired
		}
		return nil
	case protocol.StateAvailable:
		// No live lease. If this is the last receipt we handed out, the caller
		// simply paused too long; otherwise the receipt is from an old/foreign
		// delivery.
		if receipt == cur.LastReceipt {
			return ErrLeaseExpired
		}
		return ErrStaleReceipt
	case protocol.StateDead:
		return ErrMessageDead
	case protocol.StateAcked:
		return ErrAlreadyAcked
	default:
		return ErrUnknownState
	}
}

// NewExtended is the pure visibility-extension rule. CheckReceipt must already
// have passed under the same atomic transaction, which is what makes the
// extend/timeout race atomic: the deadline check and the deadline write happen
// while holding the message lock. Extension is measured from now (not from the
// old deadline); repeated extensions therefore produce strictly later
// deadlines as long as each call arrives before the current deadline.
func NewExtended(id, receipt string, gen int64, deadline time.Time, extra time.Duration, now time.Time) Event {
	return Event{
		MessageID:  id,
		Type:       EvExtended,
		Version:    protocol.Version,
		At:         now,
		ToState:    protocol.StateInvisible,
		Receipt:    receipt,
		ReceiptGen: gen,
		Deadline:   deadline,
		Extra:      extra,
	}
}

// NewAcked builds the terminal ack event. The caller already proved the receipt
// owns the live lease (CheckReceipt) inside the transaction.
func NewAcked(id, receipt string, gen int64, now time.Time) Event {
	return Event{
		MessageID:  id,
		Type:       EvAcked,
		Version:    protocol.Version,
		At:         now,
		ToState:    protocol.StateAcked,
		Receipt:    receipt,
		ReceiptGen: gen,
	}
}

// ExpireOutcome is the decision for one lapsed lease.
type ExpireOutcome struct {
	Dead     bool
	ToState  protocol.State
	Failure  *protocol.Failure
	Attempts int
}

// DecideExpire applies an expiring lease to a locked invisible message at now.
//
// now must be >= cur.Deadline (the store selects only due rows). The attempt
// that just failed is cur.Attempts (the delivery that was leased). If it was
// the MaxAttempts-th delivery the message is dead-lettered with the full
// failure history preserved; otherwise it becomes available again for a
// redelivery that will increment Attempts on its next Claim.
func DecideExpire(cur protocol.LeaseView, maxAttempts int, now time.Time) ExpireOutcome {
	f := &protocol.Failure{
		Attempt:  cur.Attempts,
		Kind:     protocol.FailLeaseExpired,
		Reason:   "worker did not confirm before visibility timeout",
		At:       now,
		Deadline: cur.Deadline,
	}
	if cur.Attempts >= maxAttempts {
		return ExpireOutcome{Dead: true, ToState: protocol.StateDead, Failure: f, Attempts: cur.Attempts}
	}
	return ExpireOutcome{Dead: false, ToState: protocol.StateAvailable, Failure: f, Attempts: cur.Attempts}
}

// NewExpireEvent builds either a lease_expired (back to available) or a
// dead_lettered event for a lapsed lease.
func NewExpireEvent(id string, cur protocol.LeaseView, o ExpireOutcome, now time.Time) Event {
	t := EvLeaseExpired
	if o.Dead {
		t = EvDeadLettered
	}
	return Event{
		MessageID: id,
		Type:      t,
		Version:   protocol.Version,
		At:        now,
		ToState:   o.ToState,
		Attempts:  o.Attempts,
		Failure:   o.Failure,
		// Retained so the redeliverable row clears its live lease fields.
		Receipt:    "",
		ReceiptGen: cur.ReceiptGen,
	}
}

// DecideNack applies an explicit negative ack with a validated live receipt.
// Like expiry, the MaxAttempts-th failed delivery dead-letters immediately;
// otherwise the message is immediately redeliverable.
func DecideNack(cur protocol.LeaseView, maxAttempts int, reason string, now time.Time) ExpireOutcome {
	f := &protocol.Failure{
		Attempt:  cur.Attempts,
		Kind:     protocol.FailNacked,
		Reason:   reason,
		At:       now,
		Deadline: cur.Deadline,
	}
	if cur.Attempts >= maxAttempts {
		return ExpireOutcome{Dead: true, ToState: protocol.StateDead, Failure: f, Attempts: cur.Attempts}
	}
	return ExpireOutcome{Dead: false, ToState: protocol.StateAvailable, Failure: f, Attempts: cur.Attempts}
}

// NewNackEvent builds the nack or dead_lettered event for a negative ack.
func NewNackEvent(id string, cur protocol.LeaseView, o ExpireOutcome, now time.Time) Event {
	t := EvNacked
	if o.Dead {
		t = EvDeadLettered
	}
	return Event{
		MessageID: id,
		Type:      t,
		Version:   protocol.Version,
		At:        now,
		ToState:   o.ToState,
		Attempts:  o.Attempts,
		Failure:   o.Failure,
		// Keep LastReceipt but clear the live lease.
		Receipt:    "",
		ReceiptGen: cur.ReceiptGen,
	}
}
