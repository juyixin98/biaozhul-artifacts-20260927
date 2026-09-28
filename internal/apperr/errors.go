// Package apperr defines the typed error contract shared by every layer of the
// service (HTTP API, director, kernel, snapshot coordinator, store and replay).
//
// Every failure that can cross a module boundary is classified into exactly
// one of four error kinds required by the project specification:
//
//   - KindInput        — invalid request / fixture input (malformed, unknown
//     reference, bad amount, unknown scenario step). The caller is at fault.
//   - KindConflict     — state conflict: an unfinished snapshot round after a
//     restart, duplicate snapshot id, marker for an aborted round, transfer
//     against a frozen account, FIFO ordering violation. Retrying the same
//     call does not help; the round must be aborted or restarted.
//   - KindExhausted    — resource exhaustion: too many concurrent snapshot
//     rounds, channel queue full, store capacity reached.
//   - KindFailure      — computation/infrastructure failure: store I/O error,
//     peer transport failure, kernel invariant violation, replay checksum
//     mismatch.
//
// Errors carry a stable machine-readable Code so tests can assert the failure
// category instead of matching on human-readable text.
package apperr

import (
	"errors"
	"fmt"
)

// Kind is the high level failure category.
type Kind string

const (
	KindInput     Kind = "input_error"
	KindConflict  Kind = "state_conflict"
	KindExhausted Kind = "resource_exhausted"
	KindFailure   Kind = "computation_failure"
)

// Stable error codes. Tests assert on these.
const (
	CodeMalformed        = "malformed"
	CodeUnknownAccount   = "unknown_account"
	CodeUnknownPeer      = "unknown_peer"
	CodeUnknownSnapshot  = "unknown_snapshot"
	CodeBadAmount        = "bad_amount"
	CodeInsufficientFund = "insufficient_funds"
	CodeUnknownScenario  = "unknown_scenario"
	CodeUnknownRun       = "unknown_run"

	CodeSnapshotInProgress = "snapshot_in_progress"
	CodeSnapshotAborted    = "snapshot_aborted"
	CodeRoundStale         = "round_stale_after_restart"
	CodeDuplicateSnapshot  = "duplicate_snapshot"
	CodeFIFOViolation      = "fifo_violation"
	CodeAccountFrozen      = "account_frozen"
	CodeNotPermitted      = "operation_not_permitted"

	CodeTooManySnapshots = "too_many_snapshots"
	CodeChannelFull      = "channel_queue_full"
	CodeStoreFull        = "store_capacity_exceeded"

	CodeStoreIO          = "store_io"
	CodeTransport        = "transport_failure"
	CodeKernelInvariant  = "kernel_invariant"
	CodeReplayMismatch   = "replay_checksum_mismatch"
	CodeFailure          = "internal_failure"
	CodeSnapshotIncomplete = "snapshot_incomplete"
)

// Error is the structured error type used across the service.
type Error struct {
	Kind Kind   // required: one of the four categories
	Code string // required: stable machine-readable code
	Op   string // optional: operation/interface where it happened, e.g. "kernel.Apply"
	Msg  string // human readable detail
	Err  error  // wrapped cause, optional
}

func (e *Error) Error() string {
	s := string(e.Kind)
	if e.Code != "" {
		s += "/" + e.Code
	}
	if e.Op != "" {
		s += " at " + e.Op
	}
	if e.Msg != "" {
		s += ": " + e.Msg
	}
	if e.Err != nil {
		s += ": " + e.Err.Error()
	}
	return s
}

func (e *Error) Unwrap() error { return e.Err }

// New builds an *Error.
func New(kind Kind, code, op, msg string, cause error) *Error {
	return &Error{Kind: kind, Code: code, Op: op, Msg: msg, Err: cause}
}

// Inputf builds an input error; helpers for the other kinds are below.
func Inputf(code, format string, args ...any) *Error {
	return &Error{Kind: KindInput, Code: code, Msg: fmt.Sprintf(format, args...)}
}

func Conflict(code, msg string) *Error {
	return &Error{Kind: KindConflict, Code: code, Msg: msg}
}

func Conflictf(code, format string, args ...any) *Error {
	return &Error{Kind: KindConflict, Code: code, Msg: fmt.Sprintf(format, args...)}
}

func Exhausted(code, msg string) *Error {
	return &Error{Kind: KindExhausted, Code: code, Msg: msg}
}

func Failure(code, op, msg string, cause error) *Error {
	return &Error{Kind: KindFailure, Code: code, Op: op, Msg: msg, Err: cause}
}

// As extracts a structured *Error from err, if present.
func As(err error) (*Error, bool) {
	var e *Error
	if errors.As(err, &e) {
		return e, true
	}
	return nil, false
}

// IsKind reports whether err is a structured error of the given kind.
func IsKind(err error, k Kind) bool {
	if e, ok := As(err); ok {
		return e.Kind == k
	}
	return false
}
