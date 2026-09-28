// Package kernel is the pure compute core of the broker.
//
// It contains no I/O: given a message snapshot, the queue rules and a time,
// it decides the next state and the event that records it. Both the in-memory
// store and the PostgreSQL store call these exact functions inside their own
// atomic transactions, so the state-machine semantics have a single source of
// truth and can be unit-tested without a database.
package kernel

import "errors"

// Closed failure taxonomy. Tests assert against these exact errors rather than
// matching on strings, and unknown states are never folded into success.
var (
	// ErrNoMessage: no message is currently available for claim.
	ErrNoMessage = errors.New("no available message")
	// ErrQueueNotFound: the referenced queue does not exist.
	ErrQueueNotFound = errors.New("queue not found")
	// ErrQueueExists: a queue with that name already exists.
	ErrQueueExists = errors.New("queue already exists")
	// ErrInvalidReceipt: empty or syntactically unknown receipt.
	ErrInvalidReceipt = errors.New("invalid receipt")
	// ErrStaleReceipt: the receipt belonged to an earlier delivery; the
	// message was redelivered to another worker and got a new receipt.
	ErrStaleReceipt = errors.New("stale receipt: message was redelivered")
	// ErrLeaseExpired: the visibility timeout elapsed before the operation;
	// the lease is no longer owned by this receipt.
	ErrLeaseExpired = errors.New("receipt lease expired: visibility timeout elapsed")
	// ErrMessageDead: the message is in the dead-letter state.
	ErrMessageDead = errors.New("message is dead-lettered")
	// ErrAlreadyAcked: the message was already confirmed.
	ErrAlreadyAcked = errors.New("message already acked")
	// ErrNotInvisible: operation requires the invisible (leased) state.
	ErrNotInvisible = errors.New("message is not in invisible state")
	// ErrUnknownState: persisted/replayed state is not one of the known states.
	ErrUnknownState = errors.New("unknown message state")
)

// InvalidArgument wraps a validation problem; the HTTP layer maps it to 400.
type InvalidArgument struct{ Msg string }

func (e *InvalidArgument) Error() string { return "invalid argument: " + e.Msg }

// IsErrorCategory reports whether err is exactly one of the broker's typed
// failures. It exists so tests/logs can record a precise failure category.
func IsErrorCategory(err error) bool {
	switch err {
	case ErrNoMessage,
		ErrQueueNotFound,
		ErrQueueExists,
		ErrInvalidReceipt,
		ErrStaleReceipt,
		ErrLeaseExpired,
		ErrMessageDead,
		ErrAlreadyAcked,
		ErrNotInvisible,
		ErrUnknownState:
		return true
	default:
		var ia *InvalidArgument
		return errors.As(err, &ia)
	}
}
