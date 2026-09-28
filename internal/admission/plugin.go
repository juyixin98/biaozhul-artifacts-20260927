// Package admission implements the ordered mutation-and-validation pipeline:
// plugin contracts, per-plugin failure policy and timeout, the mutator chain
// with convergence/idempotency guarantees, and the post-chain validators.
package admission

import (
	"context"
	"errors"
	"fmt"

	"admission/internal/types"
)

// FailurePolicy selects fail-open vs fail-close behavior when a plugin errors
// or times out. It is explicit per plugin in configuration; there is no
// implicit global default inside a plugin.
type FailurePolicy string

const (
	// FailOpen: plugin errors are tolerated and recorded; the chain continues.
	FailOpen FailurePolicy = "FailOpen"
	// FailClose: plugin errors abort the request (the category is preserved).
	FailClose FailurePolicy = "FailClose"
)

// PluginError is the structured error contract between plugins and the
// pipeline. Every plugin failure is reduced to one of the stable categories.
type PluginError struct {
	Category types.Category
	Phase    types.Phase
	Plugin   string
	Message  string
	// Timeout marks timeout-caused failures explicitly so the stored cause is
	// never ambiguous with a generic compute failure.
	Timeout bool
}

func (e *PluginError) Error() string {
	where := string(e.Phase)
	if e.Plugin != "" {
		where += "/" + e.Plugin
	}
	if e.Timeout {
		return fmt.Sprintf("%s: timeout: %s", where, e.Message)
	}
	return fmt.Sprintf("%s [%s]: %s", where, e.Category, e.Message)
}

// AsPluginError extracts a *PluginError from any error chain.
func AsPluginError(err error) (*PluginError, bool) {
	var pe *PluginError
	if errors.As(err, &pe) {
		return pe, true
	}
	return nil, false
}

func newErr(cat types.Category, phase types.Phase, plugin, msg string) *PluginError {
	return &PluginError{Category: cat, Phase: phase, Plugin: plugin, Message: msg}
}

func timeoutErr(phase types.Phase, plugin string) *PluginError {
	return &PluginError{
		Category: types.CatTimeout,
		Phase:    phase,
		Plugin:   plugin,
		Message:  "plugin did not return before its configured deadline",
		Timeout:  true,
	}
}

// Mutator transforms only the declared paths. AllowedPrefixes is enforced by
// the pipeline (never by trusting the plugin), together with the immutable
// hard guard.
type Mutator interface {
	Name() string
	AllowedPrefixes() []string
	// Mutate inspects obj (always non-nil) and returns a patch. An empty patch
	// means "no change this pass". A re-entrant call with an object already in
	// the target state MUST return an empty patch: that is the idempotency
	// contract the convergence loop relies on.
	Mutate(ctx context.Context, obj *types.Object) (types.Patch, error)
}

// Validator runs after the mutating chain has converged and returns a clean
// allow/deny decision, or an error (which is subject to its failure policy).
type Validator interface {
	Name() string
	Validate(ctx context.Context, req types.Review) (types.Decision, error)
}

// MutatorSpec pairs a configured mutator with its runtime policy.
type MutatorSpec struct {
	Plugin        Mutator
	TimeoutMS     int           // 0 => pipeline default
	FailurePolicy FailurePolicy // explicit: FailOpen | FailClose
}

// ValidatorSpec pairs a configured validator with its runtime policy.
type ValidatorSpec struct {
	Plugin        Validator
	TimeoutMS     int
	FailurePolicy FailurePolicy
}

// Config is the resolved chain configuration.
type Config struct {
	// DefaultTimeoutMS applies when a plugin does not set its own.
	DefaultTimeoutMS int
	// MaxMutationPasses bounds the convergence loop. The chain must reach a
	// fixed point well below this; exceeding it is reported as a compute
	// failure (oscillating mutator) — never an infinite loop.
	MaxMutationPasses int
	// Defaults are built-in transformers that run once, before the chain.
	Defaults   []MutatorSpec
	Mutators   []MutatorSpec
	Validators []ValidatorSpec
}
