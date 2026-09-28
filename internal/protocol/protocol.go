// Package protocol defines the wire/domain types of the local work-message
// broker: queue descriptors, message lifecycle states, receipts, failure
// records and the broker configuration of a queue.
//
// The protocol package deliberately contains no I/O and no behaviour beyond
// validation helpers: it is the vocabulary shared by the compute kernel
// (internal/kernel), the state stores (internal/storemem, internal/storepg),
// the replay view (internal/replay), the HTTP transport and the tests.
package protocol

import (
	"errors"
	"time"
)

// Version is the algorithm/protocol version emitted in logs and diagnostics so
// that a failing run can be tied to the exact state-machine revision. Bump
// whenever a state transition or receipt/attempt rule changes.
const Version = "broker-kernel-1.0.0"

// State is the lifecycle state of one message.
type State string

const (
	// StateAvailable: enqueued and eligible for Claim (no live lease).
	StateAvailable State = "available"
	// StateInvisible: claimed, leaseed exclusively to one worker until
	// Deadline; a different receipt cannot touch it.
	StateInvisible State = "invisible"
	// StateDead: terminal dead-letter state; never returned by ordinary Claim.
	StateDead State = "dead"
	// StateAcked: terminal; the message was confirmed against its live receipt.
	StateAcked State = "acked"
)

// FailureKind classifies why one delivery attempt ended without an ack.
// The taxonomy is closed so tests can assert the exact failure category.
type FailureKind string

const (
	// FailLeaseExpired: the worker paused beyond the visibility timeout and
	// the lease lapsed (nack on pause).
	FailLeaseExpired FailureKind = "lease_expired"
	// FailNacked: the consumer explicitly reported failure (negative ack).
	FailNacked FailureKind = "nacked"
)

// Failure is one complete failure reason attached to a delivery attempt.
// Failures are retained verbatim on the dead-letter copy.
type Failure struct {
	Attempt  int         `json:"attempt"`
	Kind     FailureKind `json:"kind"`
	Reason   string      `json:"reason"`
	At       time.Time   `json:"at"`
	Deadline time.Time   `json:"deadline,omitempty"`
}

// QueueConfig is the per-queue policy. VisibilityTimeout is the initial lease
// duration granted by Claim; MaxAttempts is the number of Claim-based
// deliveries allowed (1..MaxAttempts); the MaxAttempts-th failed delivery
// dead-letters the message.
type QueueConfig struct {
	Name              string        `json:"name"`
	VisibilityTimeout time.Duration `json:"visibility_timeout"`
	MaxAttempts       int           `json:"max_attempts"`
}

// Validate returns a typed InvalidArgument error for a bad queue config.
func (c QueueConfig) Validate() error {
	if c.Name == "" {
		return errors.New("queue name must not be empty")
	}
	if c.VisibilityTimeout <= 0 {
		return errors.New("visibility_timeout must be > 0")
	}
	if c.MaxAttempts < 1 {
		return errors.New("max_attempts must be >= 1")
	}
	return nil
}

// Message is the domain representation returned by stores and the HTTP API.
type Message struct {
	ID         string
	Queue      string
	Body       []byte
	State      State
	Attempts   int // number of times the message has actually been claimed
	Receipt    string
	ReceiptGen int64
	LastReceipt string
	Deadline   time.Time
	EnqueuedAt time.Time
	UpdatedAt  time.Time
	Failures   []Failure
}

// LeaseView is the state a store needs for one locked message row during a
// transition; it is also used by the pure kernel in unit tests.
type LeaseView struct {
	ID          string
	State       State
	Attempts    int
	Receipt     string
	ReceiptGen int64
	LastReceipt string
	Deadline    time.Time
}
