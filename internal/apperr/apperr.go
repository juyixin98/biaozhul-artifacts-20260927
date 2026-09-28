// Package apperr defines the cross-module error contract. Every error that
// reaches an adapter or an HTTP boundary carries a Category so that input
// errors, state conflicts, resource exhaustion and compute/storage failures
// stay distinguishable.
package apperr

import "fmt"

type Category string

const (
	// InvalidInput: malformed request payload or schema-violating config (400).
	InvalidInput Category = "invalid_input"
	// NotFound: referenced resource/schema does not exist (404).
	NotFound Category = "not_found"
	// Conflict: field ownership conflict; details carry paths and managers (409).
	Conflict Category = "conflict"
	// ResourceExhausted: payload/resource/queue limits hit (413/503).
	ResourceExhausted Category = "resource_exhausted"
	// ComputeFailure: the merge engine could not complete (500-class, but
	// distinct from storage faults so tests/dashboards can tell them apart).
	ComputeFailure Category = "compute_failure"
	// Unavailable: downstream adapter not ready (503).
	Unavailable Category = "unavailable"
	// Internal: storage corruption / unexpected driver error (500).
	Internal Category = "internal"
)

// Error is the structured error shared across modules.
type Error struct {
	Category Category
	// Code is a stable machine-readable reason within the category.
	Code    string
	Message string
	// Details carries structured context (e.g. conflict records). Optional.
	Details any
}

func (e *Error) Error() string {
	if e.Code != "" {
		return string(e.Category) + "/" + e.Code + ": " + e.Message
	}
	return string(e.Category) + ": " + e.Message
}

func New(cat Category, code, format string, args ...any) *Error {
	return &Error{Category: cat, Code: code, Message: fmt.Sprintf(format, args...)}
}

// As extracts a structured *Error from err.
func As(err error) (*Error, bool) {
	for err != nil {
		if ae, ok := err.(*Error); ok {
			return ae, true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := err.(unwrapper)
		if !ok {
			return nil, false
		}
		err = u.Unwrap()
	}
	return nil, false
}
