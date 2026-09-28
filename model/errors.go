// Package model defines the shared domain types of the path-vector
// convergence backend: reachable prefixes, path attributes, the
// deterministic route comparison order, and the typed error contract
// used across config / engine / store / replay / api.
package model

import (
	"errors"
	"fmt"
)

// Kind is a machine-readable failure category. Every error that crosses
// a package boundary carries exactly one Kind so that the HTTP layer can
// map it without inspecting error text.
type Kind string

const (
	// KindInput: malformed configuration / request payload. Caller's fault.
	KindInput Kind = "INPUT"
	// KindStateConflict: request conflicts with server-side state
	// (duplicate run id, replay against an existing run).
	KindStateConflict Kind = "STATE_CONFLICT"
	// KindResourceExhausted: a configured or hard safety budget was hit
	// (step budget, payload size, storage space).
	KindResourceExhausted Kind = "RESOURCE_EXHAUSTED"
	// KindComputeFailed: an internal invariant was violated.
	KindComputeFailed Kind = "COMPUTE_FAILED"
	// KindNotFound: referenced run/resource does not exist.
	KindNotFound Kind = "NOT_FOUND"
)

// Error is the typed error shared by all packages.
type Error struct {
	Kind    Kind
	Code    string // stable short code, e.g. LOOP_REJECTED
	Message string // human readable detail
}

func (e *Error) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("%s/%s: %s", e.Kind, e.Code, e.Message)
	}
	return fmt.Sprintf("%s: %s", e.Kind, e.Message)
}

// NewError builds a typed error.
func NewError(kind Kind, code, format string, args ...any) *Error {
	return &Error{Kind: kind, Code: code, Message: fmt.Sprintf(format, args...)}
}

// AsError extracts a *model.Error from err if present.
func AsError(err error) (*Error, bool) {
	var me *Error
	if errors.As(err, &me) {
		return me, true
	}
	return nil, false
}

// NonConvergent codes (carried inside replay.Result, not returned as Go errors).
const (
	// NonConvOscillationBudget: the engine reached max_steps and observed a
	// repeating best-route signature cycle; cycle evidence is attached.
	NonConvOscillationBudget = "OSCILLATION_BUDGET_EXCEEDED"
	// NonConvBudgetNoCycle: the engine reached max_steps without reaching a
	// fixed point and without proving a cycle (chaotic/churn or budget too small).
	NonConvBudgetNoCycle = "BUDGET_EXCEEDED_NO_CYCLE"
)

// Origin encodes the BGP ORIGIN attribute; lower value is preferred.
type Origin uint8

const (
	OriginIGP        Origin = 0
	OriginEGP        Origin = 1
	OriginIncomplete Origin = 2
)

// String renders an origin for traces/persistence.
func (o Origin) String() string {
	switch o {
	case OriginIGP:
		return "igp"
	case OriginEGP:
		return "egp"
	case OriginIncomplete:
		return "incomplete"
	default:
		return fmt.Sprintf("unknown(%d)", uint8(o))
	}
}

// ParseOrigin converts the JSON spelling ("igp"/"egp"/"incomplete").
func ParseOrigin(s string) (Origin, bool) {
	switch s {
	case "", "igp":
		return OriginIGP, true
	case "egp":
		return OriginEGP, true
	case "incomplete":
		return OriginIncomplete, true
	default:
		return 0, false
	}
}
