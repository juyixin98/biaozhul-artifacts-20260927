// Package ierr defines the cross-module error contract for the path-vector
// engine. Every error that crosses a package boundary (config, model, store,
// engine, replay, api) carries a stable Kind so callers can distinguish
// malformed input, conflicting state, resource exhaustion and computation
// failures without parsing error strings.
package ierr

import (
	"errors"
	"fmt"
)

// Kind is the stable, machine-readable category of an error.
type Kind string

const (
	// KindInvalidInput: the caller supplied malformed configuration, an
	// unknown topology reference, a malformed prefix, etc.
	KindInvalidInput Kind = "invalid_input"
	// KindStateConflict: the request contradicts stored/engine state,
	// e.g. withdrawing a candidate that was never advertised.
	KindStateConflict Kind = "state_conflict"
	// KindResourceExhausted: a hard resource limit was hit (step budget,
	// queue cap, storage failure reported as exhaustion by the backend).
	KindResourceExhausted Kind = "resource_exhausted"
	// KindComputationFailed: an unexpected internal condition (invariant
	// violation, storage I/O error that is not a limit).
	KindComputationFailed Kind = "computation_failed"
	// KindNotFound: referenced run/record does not exist.
	KindNotFound Kind = "not_found"
)

// Error is the structured error type used across all packages.
type Error struct {
	Kind   Kind
	Op     string // package/operation that raised it, e.g. "engine.announce"
	Detail string // human-readable detail
	Cause  error  // wrapped underlying error, if any
}

func (e *Error) Error() string {
	s := string(e.Kind)
	if e.Op != "" {
		s += ": " + e.Op
	}
	if e.Detail != "" {
		s += ": " + e.Detail
	}
	if e.Cause != nil {
		s += ": " + e.Cause.Error()
	}
	return s
}

func (e *Error) Unwrap() error { return e.Cause }

// New builds an error of the given kind.
func New(kind Kind, op, detail string) *Error {
	return &Error{Kind: kind, Op: op, Detail: detail}
}

// Wrap annotates an underlying error.
func Wrap(kind Kind, op, detail string, cause error) *Error {
	return &Error{Kind: kind, Op: op, Detail: detail, Cause: cause}
}

// Of returns the Kind of err, or KindComputationFailed if it is not one of
// the structured errors.
func Of(err error) Kind {
	var se *Error
	if errors.As(err, &se) {
		return se.Kind
	}
	return KindComputationFailed
}

// Is reports whether err has the given kind.
func Is(err error, k Kind) bool { return Of(err) == k }

// F is a shorthand for formatting details.
func F(format string, args ...any) string { return fmt.Sprintf(format, args...) }
