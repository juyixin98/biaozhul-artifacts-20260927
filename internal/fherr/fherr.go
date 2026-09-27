// Package fherr defines the typed error taxonomy shared by every module.
//
// The task requires four failure classes to be distinguishable wherever they
// apply: invalid input, state conflict, resource exhaustion and computation
// failure. A fifth class, unavailable (no healthy next hop), is kept separate
// instead of being folded into "computation failure" so HTTP callers can tell
// a misconfiguration apart from an operational outage.
package fherr

import (
	"errors"
	"fmt"
)

type Kind int

const (
	KindUnknown Kind = iota
	// KindInput: malformed request/config, failed validation.
	KindInput
	// KindStateConflict: optimistic/version conflict, illegal state transition
	// (e.g. marking an already-down member down), SQLite busy contention.
	KindStateConflict
	// KindResourceExhausted: storage full, connection/limit exhaustion.
	KindResourceExhausted
	// KindComputationFailed: deterministic allocation/hashing could not be
	// produced (integer overflow, replay divergence, invariant violation).
	KindComputationFailed
	// KindUnavailable: no eligible next hop for a flow.
	KindUnavailable
)

func (k Kind) String() string {
	switch k {
	case KindInput:
		return "input_error"
	case KindStateConflict:
		return "state_conflict"
	case KindResourceExhausted:
		return "resource_exhausted"
	case KindComputationFailed:
		return "computation_failed"
	case KindUnavailable:
		return "unavailable"
	default:
		return "unknown"
	}
}

// Error is the single typed error every module returns or wraps.
type Error struct {
	Kind Kind
	Op   string // short operation tag, e.g. "config.Load"
	Msg  string // human readable detail
	Err  error  // wrapped cause, may be nil
}

func (e *Error) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("%s: %s: %s: %v", e.Op, e.Kind, e.Msg, e.Err)
	}
	return fmt.Sprintf("%s: %s: %s", e.Op, e.Kind, e.Msg)
}

func (e *Error) Unwrap() error { return e.Err }

// New builds a typed error without a cause.
func New(kind Kind, op, msg string) *Error {
	return &Error{Kind: kind, Op: op, Msg: msg}
}

// Wrap annotates cause with a kind, op and message.
func Wrap(kind Kind, op, msg string, cause error) *Error {
	return &Error{Kind: kind, Op: op, Msg: msg, Err: cause}
}

// KindOf extracts the Kind of the first *Error in the chain, or KindUnknown.
func KindOf(err error) Kind {
	var fe *Error
	if errors.As(err, &fe) {
		return fe.Kind
	}
	return KindUnknown
}
