// Package protocol defines the wire- and storage-neutral domain types of the
// local work-message broker: messages, receipts, failure records, dead-letter
// reasons and the state-transition event stream.
//
// Nothing in this package touches HTTP, SQL or clocks. Layers above translate
// to/from these types; the compute kernel in internal/kernel mutates them.
package protocol

import "time"

// Status is the visibility/state of a message.
type Status string

const (
	// StatusAvailable: visible, waiting for a worker receive.
	StatusAvailable Status = "available"
	// StatusInFlight: invisible; held by a worker against a receipt until
	// the visibility deadline passes.
	StatusInFlight Status = "inflight"
	// StatusDead: permanently parked in the dead-letter area. Such messages
	// never participate in ordinary receives again.
	StatusDead Status = "dead"
	// StatusAcked: terminal; the message was confirmed by a valid receipt and
	// is retained only for audit/replay. Never returned by receive or by the
	// dead-letter listing. Keeping the row (instead of deleting it) is what
	// lets a late ack against a consumed receipt be classified as "stale"
	// rather than "unknown".
	StatusAcked Status = "acked"
)

// FailureClass is the *category* of a failed operation. Every error surface
// (HTTP status, kernel error, dead reason) maps to one of these; callers and
// tests assert the concrete class instead of checking "some error".
type FailureClass string

const (
	// FailValidation: malformed caller input (bad body, duration out of
	// range, unknown partition name shape, ...).
	FailValidation FailureClass = "validation_error"
	// FailReceiptNotFound: receipt id does not exist (never issued, or from
	// a different broker/run whose ids never existed here).
	FailReceiptNotFound FailureClass = "receipt_not_found"
	// FailReceiptConsumed: receipt already spent by an earlier ack/nack/extend
	// cycle that consumed it. Currently reserved; ack/nack consume receipts.
	FailReceiptConsumed FailureClass = "receipt_consumed"
	// FailReceiptExpired: receipt belonged to the *same* inflight message but
	// the visibility deadline elapsed before the message was redelivered.
	FailReceiptExpired FailureClass = "receipt_expired"
	// FailReceiptStale: receipt belonged to an earlier delivery; the message
	// was already redelivered (or acked/dead). This is the "late ack" class:
	// an old receipt can never confirm a later redelivery.
	FailReceiptStale FailureClass = "receipt_stale"
	// FailMessageDead: target message is in the dead-letter area.
	FailMessageDead FailureClass = "message_dead"
	// FailMessageNotFound: message id does not exist.
	FailMessageNotFound FailureClass = "message_not_found"
	// FailTimeout: caller's wait/deadline elapsed (long poll timeout).
	FailTimeout FailureClass = "timeout"
	// FailConflict: optimistic state conflict reported by the storage layer
	// (e.g. serialized transaction aborted). Callers may retry.
	FailConflict FailureClass = "conflict"
	// FailInternal: genuinely unexpected condition. Never used to paper over
	// an unknown state — such cases carry this class with full detail.
	FailInternal FailureClass = "internal_error"
)

// Message is the broker's unit of work.
type Message struct {
	ID          string
	Partition   string
	Body        []byte
	Status      Status
	Attempts    int64 // actual receive count (first receive -> 1)
	MaxAttempts int64 // attempts threshold; exceeding parks the message dead
	// AvailableAt is when an available (or re-queued) message becomes
	// eligible. Enqueue may set it into the future (delay).
	AvailableAt time.Time
	// InFlightAt / VisibilityDeadline bound the current invisible hold.
	InFlightAt         time.Time
	VisibilityDeadline time.Time
	// ReceiptID is the receipt of the *current* delivery while inflight.
	// Empty when available/dead.
	ReceiptID string
	// WorkerID is the worker holding the current delivery.
	WorkerID  string
	CreatedAt time.Time
	UpdatedAt time.Time
}

// Receipt records one issued delivery. Rows are retained after consumption so
// late acks can be classified precisely (stale vs unknown).
type Receipt struct {
	ID         string
	MessageID  string
	WorkerID   string
	IssuedAt   time.Time
	ExpiresAt  time.Time
	ExtendedAt time.Time // zero until the first successful extend
	// Consumed is true once this receipt was used to ack/nack.
	Consumed bool
}

// AttemptCause is the recorded *cause* of one failed delivery attempt. These
// are preserved verbatim in the dead-letter history and are distinct from
// FailureClass (which classifies operation-level API errors).
type AttemptCause string

const (
	// CauseVisibilityTimeout: the worker let the visibility deadline pass.
	CauseVisibilityTimeout AttemptCause = "visibility_timeout"
	// CauseWorkerNack: the worker explicitly reported failure.
	CauseWorkerNack AttemptCause = "worker_nack"
	// CauseMaxAttempts: terminal — message received MaxAttempts times and the
	// last attempt timed out.
	CauseMaxAttempts AttemptCause = "max_attempts_exhausted"
	// CauseMaxAttemptsNack: terminal — the MaxAttempts-th attempt was nacked.
	CauseMaxAttemptsNack AttemptCause = "max_attempts_exhausted_nack"
)

// AttemptFailure documents one delivery attempt outcome.
type AttemptFailure struct {
	ID         string
	MessageID  string
	Attempt    int64
	ReceiptID  string
	WorkerID   string
	Class      AttemptCause
	Reason     string
	HappenedAt time.Time
}

// DeadReason is attached to a message when it is parked in the dead-letter
// area. It preserves the *full* failure history of every attempt, not only
// the terminal error.
type DeadReason struct {
	// Class/Reason is the terminal cause (max_attempts_exhausted or
	// max_attempts_exhausted_nack).
	Class      AttemptCause
	Reason     string
	HappenedAt time.Time
	// Failures is the complete ordered attempt history (oldest first).
	Failures []AttemptFailure
}

// EventType enumerates state transitions persisted to the replay log.
type EventType string

const (
	EventEnqueued      EventType = "message_enqueued"
	EventReceived      EventType = "message_received"
	EventVisibilityExt EventType = "visibility_extended"
	EventAcked         EventType = "message_acked"
	EventNackRequeued  EventType = "nack_requeued"
	EventTimeoutRetry  EventType = "timeout_requeued"
	EventDead          EventType = "message_dead"
)

// Event is one append-only state transition. Seq is assigned by the store.
type Event struct {
	Seq       int64
	Type      EventType
	MessageID string
	Partition string
	ReceiptID string
	WorkerID  string
	At        time.Time
	Attempt   int64
	Class     AttemptCause
	Reason    string
	Version   string // protocol version that emitted the event
	// Data is type-specific structured detail (JSON), e.g. the next
	// visibility deadline or the complete dead-letter history.
	Data []byte
}
