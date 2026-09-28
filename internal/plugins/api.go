// Package plugins defines the admission plugin contract: ordered mutators and
// validators, explicit fail-open / fail-closed policy, and typed failure
// reasons. Concrete built-in plugins (defaults, capacity, stamp, schema,
// quota) and deterministic scripted fixtures used by the failure tests live
// alongside the interfaces.
package plugins

import (
	"context"
	"errors"
	"fmt"
	"time"

	"admission/internal/model"
)

// FailPolicy is declared explicitly per plugin in the startup config.
type FailPolicy string

const (
	// FailOpen: a plugin invocation error (including timeout) is recorded and
	// the chain continues without that plugin's effect.
	FailOpen FailPolicy = "open"
	// FailClosed: an invocation error denies the whole request.
	FailClosed FailPolicy = "closed"
)

// Valid parses the policy; an empty value defaults to closed (safe default).
func (p FailPolicy) Valid() bool { return p == FailOpen || p == FailClosed }

// Input is what a plugin sees for one invocation. Attempt is the 1-based pass
// number for mutators (always 1 for validators, which run once).
type Input struct {
	Request model.Request
	Doc     map[string]any
	Attempt int
}

// Mutator modifies the declared path set only, by returning PatchOps. It never
// mutates Doc itself: the framework applies the patches through the
// declared-path guard.
type Mutator interface {
	Name() string
	// DeclaredPaths lists every JSON pointer this plugin may write. Entries
	// may end in "/*" to authorize a subtree.
	DeclaredPaths() []string
	Mutate(ctx context.Context, in Input) ([]model.PatchOp, error)
	Timeout() time.Duration
	OnError() FailPolicy
}

// Verdict is a validator's policy judgement. A denial is *not* an error: it
// carries the machine-readable reason (validation_denied, resource_exhausted,
// state_conflict) that becomes the response category.
type Verdict struct {
	Allowed bool
	Reason  model.Reason
	Message string
}

// Validator judges the current document. A non-nil error means the plugin
// could not decide (timeout / compute failure / adapter failure); the
// plugin's configured fail policy then decides what happens.
type Validator interface {
	Name() string
	Validate(ctx context.Context, in Input) (Verdict, error)
	Timeout() time.Duration
	OnError() FailPolicy
}

// CallError lets a plugin attach one of the distinguishable failure reasons to
// an invocation error. Plain errors map to model.ReasonComputeFailure; a
// deadline-exceeded context maps to model.ReasonTimeout at the framework
// boundary.
type CallError struct {
	Reason model.Reason
	Err    error
}

func (e *CallError) Error() string {
	if e.Err == nil {
		return string(e.Reason)
	}
	return fmt.Sprintf("%s: %s", e.Reason, e.Err.Error())
}

func (e *CallError) Unwrap() error { return e.Err }

// Fail constructs a CallError.
func Fail(reason model.Reason, format string, args ...any) error {
	return &CallError{Reason: reason, Err: fmt.Errorf(format, args...)}
}

// AsCallError extracts a CallError from err, defaulting to compute failure.
func AsCallError(err error) (model.Reason, string) {
	var ce *CallError
	if errors.As(err, &ce) {
		return ce.Reason, ce.Error()
	}
	return model.ReasonComputeFailure, err.Error()
}
