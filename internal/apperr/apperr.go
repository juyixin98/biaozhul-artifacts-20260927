// Package apperr defines the service-wide error taxonomy.
//
// Every failure that crosses a module boundary is typed by a Kind so that
// callers (HTTP layer, tests, operators) can distinguish the four mandated
// failure classes instead of pattern-matching on error strings:
//
//   - InvalidInput       : request/configuration is malformed. Caller fixable.
//   - StateConflict      : optimistic version check failed / illegal state move.
//   - ResourceExhausted  : a bounded local resource (SQLite lock, vnode budget)
//     is currently unavailable.
//   - ComputationFailure : routing produced no usable result for structural
//     reasons (empty allocation table, cap too small).
//   - NoHealthyMember    : every candidate next-hop is down (503-class).
//
// A Code carries the machine-readable, stable sub-reason; Message is human
// oriented. Wrap with WithCause to retain an underlying error.
package apperr

import (
	"errors"
	"fmt"
	"net/http"
)

type Kind string

const (
	KindInvalidInput       Kind = "INVALID_INPUT"
	KindStateConflict      Kind = "STATE_CONFLICT"
	KindResourceExhausted  Kind = "RESOURCE_EXHAUSTED"
	KindComputationFailure Kind = "COMPUTATION_FAILURE"
	KindNoHealthyMember    Kind = "NO_HEALTHY_MEMBER"
)

// Error is the structured error used across all service modules.
type Error struct {
	Kind    Kind   `json:"kind"`
	Code    string `json:"code"`
	Message string `json:"message"`
	Cause   error  `json:"-"`
}

func New(kind Kind, code, message string) *Error {
	return &Error{Kind: kind, Code: code, Message: message}
}

func (e *Error) Error() string {
	if e.Cause != nil {
		return fmt.Sprintf("%s/%s: %s: %v", e.Kind, e.Code, e.Message, e.Cause)
	}
	return fmt.Sprintf("%s/%s: %s", e.Kind, e.Code, e.Message)
}

func (e *Error) Unwrap() error { return e.Cause }

// WithCause returns a copy of e carrying the underlying cause.
func (e *Error) WithCause(cause error) *Error {
	cp := *e
	cp.Cause = cause
	return &cp
}

// Is reports an *Error match by Kind+Code. A zero Code on either side acts as
// wildcard for that field, allowing errors.Is(err, Invalid("CODE", "")) checks.
func (e *Error) Is(target error) bool {
	t, ok := target.(*Error)
	if !ok {
		return false
	}
	if t.Kind != "" && t.Kind != e.Kind {
		return false
	}
	if t.Code != "" && t.Code != e.Code {
		return false
	}
	return true
}

// Convenience constructors. Messages passed here must state what was wrong in
// operator terms; codes are stable identifiers asserted by tests.

func Invalid(code, message string) *Error  { return New(KindInvalidInput, code, message) }
func Conflict(code, message string) *Error { return New(KindStateConflict, code, message) }
func Exhausted(code, message string) *Error {
	return New(KindResourceExhausted, code, message)
}
func Compute(code, message string) *Error { return New(KindComputationFailure, code, message) }
func NoHealthy(code, message string) *Error {
	return New(KindNoHealthyMember, code, message)
}

// As extracts a structured *Error from any error chain.
func As(err error) (*Error, bool) {
	var e *Error
	if errors.As(err, &e) {
		return e, true
	}
	return nil, false
}

// HTTPStatus maps a Kind to the HTTP status used by the API layer.
func HTTPStatus(k Kind) int {
	switch k {
	case KindInvalidInput:
		return http.StatusBadRequest
	case KindStateConflict:
		return http.StatusConflict
	case KindNoHealthyMember, KindResourceExhausted:
		return http.StatusServiceUnavailable
	case KindComputationFailure:
		// Deterministic functional failure that is not the server's fault and
		// not a malformed request body; 422 keeps it distinct from 500 bugs.
		return http.StatusUnprocessableEntity
	default:
		return http.StatusInternalServerError
	}
}
