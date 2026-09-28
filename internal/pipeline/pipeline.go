// Package pipeline is the admission core: an ordered chain of mutators
// followed by a final ordered validation phase.
//
// Guarantees enforced here:
//
//   - Mutators only modify their declared paths; they return patches and the
//     framework applies them onto a deep copy, so a rejected patch never leaves
//     a partially modified object behind ("no partial transform commits").
//   - The mutating phase iterates to a fixed point so mutually-influencing
//     plugins converge, bounded by MaxPasses: failure to converge is a
//     distinct compute failure and can never become an infinite loop.
//   - A re-entrant run is idempotent: plugins that observe their desired end
//     state emit no-op (or value-equal) patches, so the second admission of the
//     same request produces the identical final digest.
//   - Per-plugin timeout, panic and returned errors are distinguished; what
//     happens after each is governed by the plugin's explicit fail policy
//     (open = skip and continue, closed = deny). Illegal-path writes are
//     always fatal: fail-open can never authorize an out-of-scope write.
//   - Validators run exactly once, after mutation has converged.
package pipeline

import (
	"context"
	"errors"
	"fmt"
	"time"

	"admission/internal/model"
	"admission/internal/patch"
	"admission/internal/plugins"
)

// DefaultMaxPasses caps fixed-point iteration when the config omits a value.
const DefaultMaxPasses = 5

// Pipeline holds the ordered chains; it is safe for concurrent Run calls.
type Pipeline struct {
	mutators   []plugins.Mutator
	validators []plugins.Validator
	maxPasses  int
}

// New constructs a pipeline. maxPasses <= 0 selects DefaultMaxPasses.
func New(mutators []plugins.Mutator, validators []plugins.Validator, maxPasses int) *Pipeline {
	if maxPasses <= 0 {
		maxPasses = DefaultMaxPasses
	}
	return &Pipeline{mutators: mutators, validators: validators, maxPasses: maxPasses}
}

// Result is the pipeline verdict plus the full replay trail.
type Result struct {
	Allowed bool
	Reason  model.Reason
	Message string
	Final   map[string]any
	Summary *model.Summary
	Steps   []model.Step
	Patches []model.PatchOp
}

// Denied builds a denied result from a reason/message, keeping steps recorded
// so far.
func (r *Result) deny(reason model.Reason, msg string) Result {
	return Result{Allowed: false, Reason: reason, Message: msg, Steps: r.Steps, Patches: r.Patches}
}

// Run executes the full admission for req. It never panics: plugin panics are
// recovered into typed compute failures.
func (p *Pipeline) Run(ctx context.Context, req model.Request) (res Result) {
	working := req.Object
	if len(working) == 0 {
		working = req.OldObject // DELETE identity
	}
	res.Steps = []model.Step{}
	order := 0
	record := func(step model.Step) {
		order++
		step.Order = order
		res.Steps = append(res.Steps, step)
	}

	// Phase 1: mutating chain, iterated to a fixed point. DELETE carries no
	// mutable spec, so mutation is skipped entirely.
	if req.Operation != model.OpDelete {
		converged := true
		for pass := 1; pass <= p.maxPasses; pass++ {
			passChanged := false
			for _, m := range p.mutators {
				in := plugins.Input{Request: req, Doc: working, Attempt: pass}
				step, ops, invokeErr := p.runMutator(ctx, m, in)
				if invokeErr {
					record(step)
					return res.deny(step.Reason, step.Detail)
				}
				// Apply onto a copy: if the declared-path guard rejects an op,
				// the candidate is discarded and `working` stays untouched —
				// nothing partial is ever committed. The step is finalized as
				// "error" only after this guard runs, so the replay never
				// shows an illegal write as "applied".
				candidate := patch.DeepCopy(working)
				applied, applyErr := patch.Apply(candidate, ops, m.DeclaredPaths())
				if applyErr != nil {
					reason := model.ReasonComputeFailure
					if errors.Is(applyErr, patch.ErrIllegalPath) {
						reason = model.ReasonIllegalPath
					}
					step.Decision = "error"
					step.Reason = reason
					step.Detail = applyErr.Error()
					step.Patches = nil
					record(step)
					return res.deny(reason, applyErr.Error())
				}
				record(step)
				if step.Decision == "skipped-open" {
					continue // fail-open: the plugin contributed nothing
				}
				if applied {
					working = candidate
					res.Patches = append(res.Patches, ops...)
					passChanged = true
				}
			}
			if !passChanged {
				converged = true
				break
			}
			converged = false
		}
		if !converged {
			msg := fmt.Sprintf("mutator chain did not converge within %d passes (possible non-idempotent plugin)", p.maxPasses)
			return res.deny(model.ReasonComputeFailure, msg)
		}
	}

	// Phase 2: final validation, once, in declared order.
	for _, v := range p.validators {
		in := plugins.Input{Request: req, Doc: working, Attempt: 1}
		step := p.runValidator(ctx, v, in)
		record(step)
		switch step.Decision {
		case "denied":
			denied := res.deny(step.Reason, step.Detail)
			// Bind the post-mutation object to a validation denial so audits
			// show exactly what was rejected.
			if len(working) > 0 {
				if sum, serr := model.Summarize(working); serr == nil {
					denied.Final = working
					denied.Summary = &sum
				}
			}
			return denied
		case "error":
			return res.deny(step.Reason, step.Detail)
		}
	}

	res.Allowed = true
	res.Final = working
	if len(working) > 0 {
		sum, err := model.Summarize(working)
		if err != nil {
			return res.deny(model.ReasonComputeFailure, "summarize final object: "+err.Error())
		}
		res.Summary = &sum
	}
	return res
}

// runMutator invokes one mutator. The bool result marks an invocation-level
// failure that terminates the chain (timeout/panic/closed error). Illegal-path
// patches are discovered later by patch.Apply, not here.
func (p *Pipeline) runMutator(ctx context.Context, m plugins.Mutator, in plugins.Input) (model.Step, []model.PatchOp, bool) {
	start := time.Now()
	step := model.Step{Phase: model.PhaseMutate, Plugin: m.Name()}
	ops, err := invokeWithTimeout(ctx, m.Timeout(), func(callCtx context.Context) ([]model.PatchOp, error) {
		return m.Mutate(callCtx, in)
	})
	step.DurationMS = time.Since(start).Milliseconds()
	if err != nil {
		reason, msg := classify(err)
		step.Reason, step.Detail = reason, msg
		if m.OnError() == plugins.FailOpen && reason != model.ReasonIllegalPath {
			step.Decision = "skipped-open"
			return step, nil, false
		}
		step.Decision = "error"
		return step, nil, true
	}
	step.Patches = ops
	step.Decision = "applied"
	return step, ops, false
}

func (p *Pipeline) runValidator(ctx context.Context, v plugins.Validator, in plugins.Input) model.Step {
	start := time.Now()
	step := model.Step{Phase: model.PhaseValidate, Plugin: v.Name()}
	verdict, err := invokeValidatorWithTimeout(ctx, v.Timeout(), func(callCtx context.Context) (plugins.Verdict, error) {
		return v.Validate(callCtx, in)
	})
	step.DurationMS = time.Since(start).Milliseconds()
	if err != nil {
		reason, msg := classify(err)
		step.Reason, step.Detail = reason, msg
		if v.OnError() == plugins.FailOpen {
			step.Decision = "skipped-open"
			return step
		}
		step.Decision = "error"
		return step
	}
	if !verdict.Allowed {
		reason := verdict.Reason
		if reason == model.ReasonOK {
			reason = model.ReasonValidationDenied
		}
		step.Decision = "denied"
		step.Reason = reason
		step.Detail = verdict.Message
		return step
	}
	step.Decision = "applied"
	return step
}

// classify maps an invocation error to the distinguishable failure reasons.
// DeadlineExceeded (plugin timeout) is its own category; panics and other
// errors are compute failures unless the plugin tagged them.
func classify(err error) (model.Reason, string) {
	if errors.Is(err, context.DeadlineExceeded) {
		return model.ReasonTimeout, "plugin invocation timed out: " + err.Error()
	}
	if errors.Is(err, context.Canceled) {
		return model.ReasonComputeFailure, "plugin invocation canceled: " + err.Error()
	}
	if pe, ok := isPanic(err); ok {
		return model.ReasonComputeFailure, "plugin panicked: " + pe
	}
	reason, detail := plugins.AsCallError(err)
	return reason, detail
}
