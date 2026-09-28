// Package protocol defines the wire-level message envelope and the verdict
// vocabulary shared by the causal broadcast core, the HTTP transport and the
// independent test oracle.
//
// The package intentionally holds only data types and self-contained envelope
// validation: it contains no delivery algorithm, so the reference oracle can
// import these types without importing any logic whose correctness is under
// test.
package protocol

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"
)

// Version is the protocol/implementation version stamped into logs, status
// endpoints and test run records. Bump on any semantic change.
const Version = "cbcast-1.0.0"

// VC is a vector clock keyed by fixed-membership node ID.
type VC = map[string]uint64

// Envelope is the unit of broadcast: one local event, or one network message
// carrying that event.
//
// Identity: MessageID is always "<sender>:<clock[sender]>" (the sender's own
// sequence number at broadcast). Together with the fixed membership this gives
// every event a globally unique, content-independent identity, which is what
// duplicate detection keys on.
type Envelope struct {
	MessageID   string    `json:"message_id"`
	Sender      string    `json:"sender"`
	Clock       VC        `json:"clock"`
	Payload     []byte    `json:"payload"`
	PayloadHash string    `json:"payload_hash"`
	CreatedAt   time.Time `json:"created_at"`
}

// VerdictKind is the exact, machine-readable category of an ingest decision.
// Unknown/error conditions never collapse into "delivered".
type VerdictKind string

const (
	// VerdictDelivered: the message (and possibly a causal cascade of
	// previously buffered messages) is now delivered.
	VerdictDelivered VerdictKind = "delivered"
	// VerdictBuffered: received but causally not ready; held with explicit
	// missing-predecessor reasons.
	VerdictBuffered VerdictKind = "buffered"
	// VerdictDuplicate: byte-content identical envelope already known.
	VerdictDuplicate VerdictKind = "duplicate"
	// VerdictConflict: same MessageID already known with different content.
	VerdictConflict VerdictKind = "conflict"
	// VerdictRejected: malformed envelope (validation failure).
	VerdictRejected VerdictKind = "rejected"
	// VerdictOverflow: buffer at capacity; backpressure, message NOT stored.
	VerdictOverflow VerdictKind = "overflow"
)

// Gap identifies one missing predecessor: the message from node Sender with
// sender-sequence number Seq has not been delivered yet.
type Gap struct {
	Sender string `json:"sender"`
	Seq    uint64 `json:"seq"`
}

func (g Gap) String() string { return fmt.Sprintf("%s:%d", g.Sender, g.Seq) }

// IngestResult is the core's verdict for one ingest attempt.
type IngestResult struct {
	Verdict    VerdictKind    `json:"verdict"`
	MessageID  string         `json:"message_id"`
	AttemptID  string         `json:"attempt_id"`
	WaitingFor []Gap          `json:"waiting_for,omitempty"`
	// Delivered contains the triggering envelope first (when it is delivered
	// now) followed by every buffered envelope released in the causal cascade,
	// in this node's local delivery order.
	Delivered  []*Envelope `json:"delivered,omitempty"`
	Reason     string      `json:"reason,omitempty"`
	BufferUsed int         `json:"buffer_used"`
	BufferCap  int         `json:"buffer_cap"`
}

// ValidationError describes why an envelope is malformed. Error codes are
// stable so tests can assert the failure category, not just failure/non-failure.
type ValidationError struct {
	Code    string
	Message string
}

func (e *ValidationError) Error() string { return e.Code + ": " + e.Message }

// Standard validation failure categories.
const (
	ValBadEncoding = "bad_encoding"
	ValNonMember   = "non_member_sender"
	ValBadID       = "bad_message_id"
	ValBadClock    = "bad_clock"
	ValBadHash     = "bad_payload_hash"
	ValBadTime     = "bad_timestamp"
	ValOversize    = "payload_oversize"
)

// ComputePayloadHash returns the hex SHA-256 of the payload.
func ComputePayloadHash(payload []byte) string {
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

// ExpectedMessageID returns the canonical identity for an event from sender
// with the given sender-sequence number.
func ExpectedMessageID(sender string, senderSeq uint64) string {
	return fmt.Sprintf("%s:%d", sender, senderSeq)
}

// Validate checks envelope well-formedness against the fixed member set and
// local limits. It is pure (no I/O). It returns a *ValidationError whose Code
// is one of the Val* constants, or nil.
//
// It deliberately performs every check even after an early failure is known
// (accumulating) only when checks are independent; deterministic single-code
// ordering is documented in docs/SEMANTICS.md.
func Validate(e *Envelope, members map[string]bool, maxPayloadBytes int) error {
	if e == nil {
		return &ValidationError{Code: ValBadEncoding, Message: "nil envelope"}
	}
	if e.Sender == "" {
		return &ValidationError{Code: ValNonMember, Message: "empty sender"}
	}
	if !members[e.Sender] {
		return &ValidationError{Code: ValNonMember, Message: fmt.Sprintf("sender %q is not in the fixed membership", e.Sender)}
	}
	if e.Clock == nil {
		return &ValidationError{Code: ValBadClock, Message: "clock is absent"}
	}
	senderSeq, ok := e.Clock[e.Sender]
	if !ok || senderSeq == 0 {
		return &ValidationError{Code: ValBadClock, Message: fmt.Sprintf("clock must carry sender %q component >= 1", e.Sender)}
	}
	for node := range e.Clock {
		if !members[node] {
			return &ValidationError{Code: ValBadClock, Message: fmt.Sprintf("clock references non-member node %q", node)}
		}
	}
	if want := ExpectedMessageID(e.Sender, senderSeq); e.MessageID != want {
		return &ValidationError{Code: ValBadID, Message: fmt.Sprintf("message_id %q does not match identity %q", e.MessageID, want)}
	}
	if e.PayloadHash == "" {
		return &ValidationError{Code: ValBadHash, Message: "payload_hash is absent"}
	}
	if want := ComputePayloadHash(e.Payload); e.PayloadHash != want {
		return &ValidationError{Code: ValBadHash, Message: "payload_hash does not match SHA-256(payload)"}
	}
	if e.CreatedAt.IsZero() {
		return &ValidationError{Code: ValBadTime, Message: "created_at must not be zero"}
	}
	if maxPayloadBytes > 0 && len(e.Payload) > maxPayloadBytes {
		return &ValidationError{Code: ValOversize, Message: fmt.Sprintf("payload %d bytes exceeds limit %d", len(e.Payload), maxPayloadBytes)}
	}
	return nil
}

// AsValidationError unwraps a validation error.
func AsValidationError(err error) (*ValidationError, bool) {
	var ve *ValidationError
	if errors.As(err, &ve) {
		return ve, true
	}
	return nil, false
}
