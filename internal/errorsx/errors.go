// Package errorsx defines the typed error categories that flow across module
// boundaries. Callers classify failures with Code instead of matching strings.
//
//	CategoryInput       — malformed / invalid request or spec
//	CategoryState       — state conflict: drift detected, plan bound to a stale
//	                       observation, resource in non-terminal lifecycle state
//	CategoryExhausted   — resource exhausted: quota/capacity in the environment
//	CategoryCompute     — planning computation failed (cycle, bad reference,
//	                       immutable diff misclassified, dependency ordering bug)
//	CategoryProtected   — destruction of a protected resource without release
//	CategoryTransient   — adapter/provider transient fault (retryable)
//	CategoryUnknown     — provider gave an ambiguous response (create may have landed)
package errorsx

import (
	"errors"
	"fmt"
)

type Category string

const (
	CategoryInput     Category = "input_error"
	CategoryState     Category = "state_conflict"
	CategoryExhausted Category = "resource_exhausted"
	CategoryCompute   Category = "compute_failure"
	CategoryProtected Category = "protection_denied"
	CategoryTransient Category = "transient"
	CategoryUnknown   Category = "unknown_commit"
)

// Error is the structured error carried across module boundaries.
type Error struct {
	Cat  Category
	Code string // stable machine code, e.g. "DRIFT_DETECTED"
	Msg  string
	// Evidence carries run/step identifiers usable for replay.
	Evidence map[string]any
}

func (e *Error) Error() string {
	if len(e.Evidence) == 0 {
		return fmt.Sprintf("%s/%s: %s", e.Cat, e.Code, e.Msg)
	}
	return fmt.Sprintf("%s/%s: %s (%v)", e.Cat, e.Code, e.Msg, e.Evidence)
}

// New builds an error in a category.
func New(cat Category, code, msg string, ev map[string]any) *Error {
	return &Error{Cat: cat, Code: code, Msg: msg, Evidence: ev}
}

// AsError extracts *Error if present.
func AsError(err error) (*Error, bool) {
	var e *Error
	if errors.As(err, &e) {
		return e, true
	}
	return nil, false
}

// CategoryOf returns the category, or CategoryUnknown for untyped errors.
func CategoryOf(err error) Category {
	if e, ok := AsError(err); ok {
		return e.Cat
	}
	return CategoryUnknown
}

// Convenience constructors ---------------------------------------------------

func Input(code, msg string, ev map[string]any) *Error {
	return New(CategoryInput, code, msg, ev)
}
func State(code, msg string, ev map[string]any) *Error {
	return New(CategoryState, code, msg, ev)
}
func Exhausted(code, msg string, ev map[string]any) *Error {
	return New(CategoryExhausted, code, msg, ev)
}
func Compute(code, msg string, ev map[string]any) *Error {
	return New(CategoryCompute, code, msg, ev)
}
func Protected(code, msg string, ev map[string]any) *Error {
	return New(CategoryProtected, code, msg, ev)
}
func Transient(code, msg string, ev map[string]any) *Error {
	return New(CategoryTransient, code, msg, ev)
}
func UnknownCommit(code, msg string, ev map[string]any) *Error {
	return New(CategoryUnknown, code, msg, ev)
}
