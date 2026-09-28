package plugins

import (
	"context"
	"sync/atomic"
	"time"

	"admission/internal/model"
)

// This file contains deterministic *scripted* plugins used exclusively by the
// failure tests. They are intentionally not registered by the default startup
// config: a real deployment wires the built-in plugins, tests wire these to
// force specific failure categories (timeout distinguished from compute
// failure, fail-open vs fail-closed, and illegal-path writes).

// ScriptKind enumerates the canned behaviors a ScriptedMutator can play.
type ScriptKind string

const (
	// ScriptTimeout sleeps past the invocation deadline.
	ScriptTimeout ScriptKind = "timeout"
	// ScriptPanic panics; the framework recovers it as compute_failure.
	ScriptPanic ScriptKind = "panic"
	// ScriptComputeError returns a plain compute_failure error.
	ScriptComputeError ScriptKind = "compute_error"
	// ScriptIllegalPath emits a patch outside its declared path set.
	ScriptIllegalPath ScriptKind = "illegal_path"
	// ScriptOscillate alternates between two values on every invocation, so
	// the mutating chain can never reach a fixed point. Used to prove the
	// convergence cap prevents infinite loops and surfaces a distinct
	// compute_failure verdict.
	ScriptOscillate ScriptKind = "oscillate"
	// ScriptStatic emits a fixed patch once; the fixture used for
	// illegal-path-independent, assertable success chains.
	ScriptStatic ScriptKind = "static"
	// ScriptExhausted (quota fixture only) returns ErrQuotaExhausted.
	ScriptExhausted ScriptKind = "exhausted"
)

// ScriptedMutator is configured per test case.
type ScriptedMutator struct {
	PluginName  string
	Kind        ScriptKind
	Paths       []string
	Policy      FailPolicy
	CallTimeout time.Duration
	// StaticOp is emitted for ScriptStatic.
	StaticOp model.PatchOp
	// calls is observable for tests asserting re-entry idempotency behavior.
	calls atomic.Int64
}

func (m *ScriptedMutator) Name() string {
	if m.PluginName == "" {
		return "scripted-" + string(m.Kind)
	}
	return m.PluginName
}
func (m *ScriptedMutator) DeclaredPaths() []string { return m.Paths }
func (m *ScriptedMutator) Timeout() time.Duration  { return m.CallTimeout }
func (m *ScriptedMutator) OnError() FailPolicy     { return m.Policy }
func (m *ScriptedMutator) Calls() int64            { return m.calls.Load() }

func (m *ScriptedMutator) Mutate(ctx context.Context, _ Input) ([]model.PatchOp, error) {
	m.calls.Add(1)
	switch m.Kind {
	case ScriptTimeout:
		// Respect cancellation but sleep longer than the configured timeout so
		// the framework's deadline wins.
		select {
		case <-time.After(2 * m.CallTimeout):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return nil, ctx.Err()
	case ScriptPanic:
		panic("scripted mutator panic")
	case ScriptComputeError:
		return nil, Fail(model.ReasonComputeFailure, "scripted compute failure")
	case ScriptIllegalPath:
		return []model.PatchOp{{Op: "replace", Path: "/metadata/name", Value: "hijacked"}}, nil
	case ScriptOscillate:
		if m.calls.Load()%2 == 1 {
			return []model.PatchOp{{Op: "add", Path: "/spec/osc", Value: int64(1)}}, nil
		}
		return []model.PatchOp{{Op: "replace", Path: "/spec/osc", Value: int64(2)}}, nil
	case ScriptStatic:
		return []model.PatchOp{m.StaticOp}, nil
	default:
		return nil, Fail(model.ReasonComputeFailure, "unknown script kind %q", m.Kind)
	}
}

// ScriptedValidator plays canned validator behaviors.
type ScriptedValidator struct {
	PluginName string
	// ErrorKind when non-empty forces an invocation error (timeout/panic/...).
	ErrorKind ScriptKind
	// DenyReason when non-empty forces a policy denial verdict.
	DenyReason  model.Reason
	DenyMsg     string
	Policy      FailPolicy
	CallTimeout time.Duration
}

func (v *ScriptedValidator) Name() string {
	if v.PluginName == "" {
		return "scripted-validator-" + string(v.ErrorKind)
	}
	return v.PluginName
}
func (v *ScriptedValidator) Timeout() time.Duration { return v.CallTimeout }
func (v *ScriptedValidator) OnError() FailPolicy    { return v.Policy }

func (v *ScriptedValidator) Validate(ctx context.Context, _ Input) (Verdict, error) {
	switch v.ErrorKind {
	case ScriptTimeout:
		select {
		case <-time.After(2 * v.CallTimeout):
		case <-ctx.Done():
			return Verdict{}, ctx.Err()
		}
		return Verdict{}, ctx.Err()
	case ScriptPanic:
		panic("scripted validator panic")
	case ScriptComputeError:
		return Verdict{}, Fail(model.ReasonComputeFailure, "scripted validator compute failure")
	}
	if v.DenyReason != "" {
		return Verdict{Allowed: false, Reason: v.DenyReason, Message: v.DenyMsg}, nil
	}
	return Verdict{Allowed: true}, nil
}

var _ Mutator = (*ScriptedMutator)(nil)
var _ Validator = (*ScriptedValidator)(nil)
