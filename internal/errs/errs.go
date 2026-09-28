// Package errs defines the cross-module error contract. Every failure that can
// cross an HTTP boundary carries a Class so that tests (and clients) can
// distinguish bad input, state conflicts, interrupted snapshots, resource
// exhaustion and compute failures instead of string-matching messages.
package errs

import (
	"errors"
	"fmt"
	"net/http"
)

type Class string

const (
	// ClassInputInvalid: malformed request, unknown peer, bad amount.
	ClassInputInvalid Class = "input_invalid"
	// ClassComputeFailure: the request is well-formed but the token kernel
	// rejects it (e.g. insufficient funds).
	ClassComputeFailure Class = "compute_failure"
	// ClassStateConflict: request conflicts with protocol state (duplicate
	// session, FIFO reordering, late/incompatible transition).
	ClassStateConflict Class = "state_conflict"
	// ClassSnapshotInterrupted: a snapshot that was recording when the process
	// restarted. It can only fail or be abandoned - never stitched together
	// from states taken at different times.
	ClassSnapshotInterrupted Class = "snapshot_interrupted"
	// ClassResourceExhausted: bounded resource full (outbox cap, body size).
	ClassResourceExhausted Class = "resource_exhausted"
	// ClassUnavailable: a dependency (peer, database) is not reachable.
	ClassUnavailable Class = "unavailable"
)

// Stable machine-readable codes.
const (
	CodeMalformed          = "malformed_message"
	CodeUnknownPeer        = "unknown_peer"
	CodeBadAmount          = "bad_amount"
	CodeBodyTooLarge       = "body_too_large"
	CodeInsufficientFunds  = "insufficient_funds"
	CodeSessionExists      = "session_exists"
	CodeSessionUnknown     = "session_unknown"
	CodeSessionAborted     = "session_aborted"
	CodeFIFOViolation      = "fifo_order_violation"
	CodeLocalStateExists   = "local_state_exists"
	CodeOutboxFull         = "outbox_full"
	CodePeerUnavailable    = "peer_unavailable"
	CodeSnapshotInterrupted = "snapshot_interrupted_restart"
)

type Error struct {
	Class   Class  `json:"class"`
	Code    string `json:"code"`
	Message string `json:"message"`
	RunID   string `json:"run_id,omitempty"`
	Err     error  `json:"-"`
}

func (e *Error) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("%s/%s: %s: %v", e.Class, e.Code, e.Message, e.Err)
	}
	return fmt.Sprintf("%s/%s: %s", e.Class, e.Code, e.Message)
}

func (e *Error) Unwrap() error { return e.Err }

func New(class Class, code, msg string, cause error) *Error {
	return &Error{Class: class, Code: code, Message: msg, Err: cause}
}

// HTTPStatus maps each class to a status. Distinct classes never share a
// generic 400/500, so failure categories stay observable over the API.
func (e *Error) HTTPStatus() int {
	switch e.Class {
	case ClassInputInvalid:
		return http.StatusBadRequest
	case ClassComputeFailure:
		return http.StatusUnprocessableEntity
	case ClassStateConflict:
		return http.StatusConflict
	case ClassSnapshotInterrupted:
		return http.StatusConflict
	case ClassResourceExhausted:
		return http.StatusTooManyRequests
	case ClassUnavailable:
		return http.StatusServiceUnavailable
	default:
		return http.StatusInternalServerError
	}
}

func As(err error) (*Error, bool) {
	var e *Error
	if errors.As(err, &e) {
		return e, true
	}
	return nil, false
}
