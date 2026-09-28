package admission

import (
	"context"
	"fmt"
	"time"

	"admission/internal/jsonpatch"
	"admission/internal/types"
)

// hardGuardedPaths can never be written by any mutator. Defaulting plugins are
// also subject to it: the immutable flag is only set by the service after a
// successful CREATE, never through a patch.
var hardGuardedPaths = []string{"/spec/immutable"}

// LogEntry is one replay-grade record emitted while a review is processed.
// The service funnels entries into the run logger and audit store.
type LogEntry struct {
	RunID      string
	Time       time.Time
	Level      string // INFO | DENY | ERROR
	Phase      types.Phase
	Pass       int
	Plugin     string
	Message    string
	Category   types.Category
	BeforeHash string
	AfterHash  string
	Patch      []types.PatchOp
	Extra      map[string]any
}

// RunLogger sinks structured per-run entries. Implementations must be safe for
// concurrent use.
type RunLogger interface {
	Log(e LogEntry)
}

// noopLogger is used when the caller does not supply a logger.
type noopLogger struct{}

func (noopLogger) Log(LogEntry) {}

// Pipeline executes one review at a time. It is stateless across reviews; the
// service layer owns idempotency, persistence and reconciliation.
type Pipeline struct {
	cfg    Config
	logger RunLogger
	now    func() time.Time
}

// New constructs a pipeline. Missing configuration values are filled with
// safe defaults (see defaults.go); a pipeline with no validators is rejected
// because the chain's result would never be finally validated.
func New(cfg Config, logger RunLogger) (*Pipeline, error) {
	cfg = withDefaults(cfg)
	if err := validateConfig(cfg); err != nil {
		return nil, err
	}
	if logger == nil {
		logger = noopLogger{}
	}
	return &Pipeline{cfg: cfg, logger: logger, now: time.Now}, nil
}

func validateConfig(cfg Config) error {
	if len(cfg.Validators) == 0 {
		return fmt.Errorf("admission: pipeline requires at least one validator (final validation is mandatory)")
	}
	seen := map[string]bool{}
	check := func(name, pol string, timeout int) error {
		if name == "" {
			return fmt.Errorf("admission: plugin name must not be empty")
		}
		if seen[name] {
			return fmt.Errorf("admission: duplicate plugin name %q", name)
		}
		seen[name] = true
		switch FailurePolicy(pol) {
		case FailOpen, FailClose:
		default:
			return fmt.Errorf("admission: plugin %q has unknown failure policy %q", name, pol)
		}
		if timeout < 0 {
			return fmt.Errorf("admission: plugin %q has negative timeout", name)
		}
		return nil
	}
	for _, d := range cfg.Defaults {
		if err := check(d.Plugin.Name(), string(d.FailurePolicy), d.TimeoutMS); err != nil {
			return err
		}
	}
	for _, m := range cfg.Mutators {
		if err := check(m.Plugin.Name(), string(m.FailurePolicy), m.TimeoutMS); err != nil {
			return err
		}
	}
	for _, v := range cfg.Validators {
		if err := check(v.Plugin.Name(), string(v.FailurePolicy), v.TimeoutMS); err != nil {
			return err
		}
	}
	return nil
}

// RunResult is the raw pipeline outcome; the service wraps it into a
// types.Response and persists it.
type RunResult struct {
	Object  types.Object
	Allowed bool
	Deny    string
	Steps   []types.Step
	Err     *PluginError
}

// Run executes: defaults (once) → ordered mutators to a fixed point → final
// validators. On any failure it returns RunResult.Err and the steps collected
// up to the failure. The returned object must be ignored when Err != nil:
// patches are applied to a working copy and a failed run commits nothing.
func (p *Pipeline) Run(ctx context.Context, runID string, req types.Review) RunResult {
	start := p.now()
	_ = start
	steps := make([]types.Step, 0, 16)
	log := func(lvl string, phase types.Phase, pass int, name, msg string, cat types.Category) {
		p.logger.Log(LogEntry{
			RunID: runID, Time: p.now(), Level: lvl, Phase: phase,
			Pass: pass, Plugin: name, Message: msg, Category: cat,
		})
	}

	work := req.Object // value copy; patches operate on a generic-tree copy
	normalizeObject(&work)
	beforeHash := work.Fingerprint()
	log("INFO", types.PhaseDefault, 0, "-",
		fmt.Sprintf("run start op=%s kind=%s uid=%s before=%s", req.Operation, req.Object.Kind, req.UID, beforeHash), types.CatNone)

	// 1) Built-in defaulting transformers — exactly once, in order.
	for i, spec := range p.cfg.Defaults {
		res, err := p.applyMutator(ctx, runID, &work, spec, types.PhaseDefault, 1, i, &steps, log)
		if err != nil {
			return RunResult{Object: req.Object, Steps: steps, Err: err}
		}
		_ = res
	}

	// 2) Ordered mutator chain to a fixed point. A pass starts from hash H0 and
	// runs every mutator in declared order; convergence is when the pass ends at
	// H0 again. Passes are bounded, so even an oscillating plugin terminates as
	// a compute failure — by construction no loop can run forever.
	converged := false
	for pass := 1; pass <= p.cfg.MaxMutationPasses; pass++ {
		passStart := work.Fingerprint()
		for i, spec := range p.cfg.Mutators {
			changed, err := p.applyMutator(ctx, runID, &work, spec, types.PhaseMutating, pass, i, &steps, log)
			if err != nil {
				return RunResult{Object: req.Object, Steps: steps, Err: err}
			}
			_ = changed
		}
		if work.Fingerprint() == passStart {
			converged = true
			break
		}
	}
	if !converged {
		err := newErr(types.CatComputeFailure, types.PhaseMutating, "-",
			fmt.Sprintf("mutating chain did not converge within %d passes (oscillating plugins)", p.cfg.MaxMutationPasses))
		log("ERROR", types.PhaseMutating, p.cfg.MaxMutationPasses, "-", err.Message, err.Category)
		return RunResult{Object: req.Object, Steps: steps, Err: err}
	}
	log("INFO", types.PhaseMutating, 0, "-",
		fmt.Sprintf("mutation converged after passes, after=%s", work.Fingerprint()), types.CatNone)

	// 3) Final validation — mandatory, ordered, after all mutations.
	for i, spec := range p.cfg.Validators {
		decision, err := p.runValidator(ctx, runID, req, &work, spec, i, &steps, log)
		if err != nil {
			return RunResult{Object: req.Object, Steps: steps, Err: err}
		}
		if !decision.Allowed {
			log("DENY", types.PhaseValidating, 0, spec.Plugin.Name(), decision.Reason, types.CatNone)
			return RunResult{Object: work, Allowed: false, Deny: decision.Reason, Steps: steps}
		}
	}
	log("INFO", types.PhaseValidating, 0, "-",
		fmt.Sprintf("all validators passed, final=%s", work.Fingerprint()), types.CatNone)
	return RunResult{Object: work, Allowed: true, Steps: steps}
}

// applyMutator invokes one mutator under a deadline, enforces declared-path
// and hard-guard restrictions, and applies the whole emitted patch atomically
// to the working copy. It reports whether the object changed.
func (p *Pipeline) applyMutator(
	ctx context.Context, runID string, work *types.Object,
	spec MutatorSpec, phase types.Phase, pass, index int,
	steps *[]types.Step, log func(string, types.Phase, int, string, string, types.Category),
) (bool, *PluginError) {
	name := spec.Plugin.Name()
	timeout := p.timeoutFor(spec.TimeoutMS)

	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	started := p.now()
	before := work.Fingerprint()

	type mutOut struct {
		patch types.Patch
		err   error
	}
	ch := make(chan mutOut, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				ch <- mutOut{err: fmt.Errorf("plugin panicked: %v", r)}
			}
		}()
		patch, err := spec.Plugin.Mutate(cctx, work)
		ch <- mutOut{patch: patch, err: err}
	}()

	var patch types.Patch
	var perr error
	select {
	case out := <-ch:
		patch, perr = out.patch, out.err
	case <-cctx.Done():
		perr = context.DeadlineExceeded
	case <-ctx.Done():
		return false, newErr(types.CatComputeFailure, phase, name, "pipeline context cancelled")
	}
	elapsed := p.now().Sub(started)

	step := types.Step{
		Pass: pass, Index: index, Phase: phase, Plugin: name,
		StartedAt: started, ElapsedMS: elapsed.Milliseconds(), BeforeHash: before,
	}
	defer func() { *steps = append(*steps, step) }()

	if perr != nil {
		cat := types.CatComputeFailure
		timedOut := false
		msg := perr.Error()
		if pe, ok := AsPluginError(perr); ok {
			cat, msg = pe.Category, pe.Message
		}
		if perr == context.DeadlineExceeded || ctxErrIsTimeout(perr) {
			te := timeoutErr(phase, name)
			cat, msg, timedOut = te.Category, te.Message, true
		}
		step.Message = msg
		step.Category = cat
		if handled := p.handleFailure(spec.FailurePolicy, phase, name, msg, cat, timedOut, &step, log); handled != nil {
			return false, handled
		}
		return false, nil
	}

	// Authorization of the patch happens here, not in the plugin: declared
	// prefixes + immutable hard guard + concrete path validity.
	doc, err := jsonpatch.DocumentOf(work)
	if err != nil {
		ie := newErr(types.CatComputeFailure, phase, name, "encode object: "+err.Error())
		step.Message = ie.Message
		step.Category = ie.Category
		if handled := p.handleFailure(spec.FailurePolicy, phase, name, ie.Message, ie.Category, false, &step, log); handled != nil {
			return false, handled
		}
		return false, nil
	}
	changed := false
	for _, op := range patch.Ops {
		if isGuarded(op.Path) {
			ie := newErr(types.CatIllegalMutation, phase, name,
				fmt.Sprintf("op %s targets hard-guarded path %q", op.Op, op.Path))
			step.Message = ie.Message
			step.Category = ie.Category
			log("ERROR", phase, pass, name, ie.Message, ie.Category)
			return false, ie // guard violations always fail-close
		}
		ptr, err := jsonpatch.Parse(op.Path)
		if err != nil {
			ie := newErr(types.CatIllegalMutation, phase, name, "illegal path: "+err.Error())
			log("ERROR", phase, pass, name, ie.Message, ie.Category)
			return false, ie
		}
		if !ptr.IsBelow(spec.Plugin.AllowedPrefixes()) {
			ie := newErr(types.CatIllegalMutation, phase, name,
				fmt.Sprintf("op %s %q is outside declared prefixes %v", op.Op, op.Path, spec.Plugin.AllowedPrefixes()))
			log("ERROR", phase, pass, name, ie.Message, ie.Category)
			return false, ie
		}
		nd, applyErr := jsonpatch.Apply(doc, op)
		if applyErr != nil {
			ie := newErr(types.CatIllegalMutation, phase, name,
				fmt.Sprintf("cannot apply %s %q: %v", op.Op, op.Path, applyErr))
			log("ERROR", phase, pass, name, ie.Message, ie.Category)
			return false, ie
		}
		doc = nd.(map[string]any)
		changed = true
	}

	if changed {
		var next types.Object
		if err := jsonpatch.ObjectFrom(doc, &next); err != nil {
			ie := newErr(types.CatComputeFailure, phase, name, "decode object: "+err.Error())
			return false, ie
		}
		*work = next
	}
	after := work.Fingerprint()
	step.Mutated = changed && after != before
	step.Patch = patch.Ops
	step.AfterHash = after
	step.Allowed = boolPtr(true)
	if step.Mutated {
		log("INFO", phase, pass, name,
			fmt.Sprintf("applied %d op(s): %s -> %s", len(patch.Ops), before, after), types.CatNone)
	}
	return step.Mutated, nil
}

func (p *Pipeline) runValidator(
	ctx context.Context, runID string, req types.Review, work *types.Object,
	spec ValidatorSpec, index int, steps *[]types.Step,
	log func(string, types.Phase, int, string, string, types.Category),
) (types.Decision, *PluginError) {
	name := spec.Plugin.Name()
	timeout := p.timeoutFor(spec.TimeoutMS)
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	started := p.now()
	before := work.Fingerprint()

	type valOut struct {
		dec types.Decision
		err error
	}
	ch := make(chan valOut, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				ch <- valOut{err: fmt.Errorf("plugin panicked: %v", r)}
			}
		}()
		dec, err := spec.Plugin.Validate(cctx, reqWithObject(req, *work))
		ch <- valOut{dec: dec, err: err}
	}()

	var dec types.Decision
	var perr error
	select {
	case out := <-ch:
		dec, perr = out.dec, out.err
	case <-cctx.Done():
		perr = context.DeadlineExceeded
	case <-ctx.Done():
		return types.Decision{}, newErr(types.CatComputeFailure, types.PhaseValidating, name, "pipeline context cancelled")
	}
	elapsed := p.now().Sub(started)
	step := types.Step{
		Pass: 1, Index: index, Phase: types.PhaseValidating, Plugin: name,
		StartedAt: started, ElapsedMS: elapsed.Milliseconds(), BeforeHash: before, AfterHash: before,
	}
	defer func() { *steps = append(*steps, step) }()

	if perr != nil {
		cat := types.CatComputeFailure
		timedOut := false
		msg := perr.Error()
		if pe, ok := AsPluginError(perr); ok {
			cat, msg = pe.Category, pe.Message
		}
		if perr == context.DeadlineExceeded || ctxErrIsTimeout(perr) {
			te := timeoutErr(types.PhaseValidating, name)
			cat, msg, timedOut = te.Category, te.Message, true
		}
		step.Message = msg
		step.Category = cat
		if handled := p.handleFailure(spec.FailurePolicy, types.PhaseValidating, name, msg, cat, timedOut, &step, log); handled != nil {
			return types.Decision{}, handled
		}
		// Fail-open: tolerate and continue.
		return types.Decision{Allowed: true, Reason: "tolerated:" + name}, nil
	}
	allowed := dec.Allowed
	step.Allowed = &allowed
	step.Message = dec.Reason
	if allowed {
		log("INFO", types.PhaseValidating, 0, name, "allowed", types.CatNone)
	}
	return dec, nil
}

// handleFailure centralizes the explicit open/close decision. A non-nil return
// is the terminal pipeline error; nil means the failure was tolerated.
func (p *Pipeline) handleFailure(
	policy FailurePolicy, phase types.Phase, name, msg string, cat types.Category, timedOut bool,
	step *types.Step, log func(string, types.Phase, int, string, string, types.Category),
) *PluginError {
	if policy == FailOpen {
		step.Tolerated = true
		log("INFO", phase, step.Pass, name,
			fmt.Sprintf("failure tolerated (FailOpen): [%s] %s", cat, msg), cat)
		return nil
	}
	pe := &PluginError{Category: cat, Phase: phase, Plugin: name, Message: msg, Timeout: timedOut}
	log("ERROR", phase, step.Pass, name,
		fmt.Sprintf("failure aborts request (FailClose): [%s] %s", cat, msg), cat)
	return pe
}

func (p *Pipeline) timeoutFor(pluginMS int) time.Duration {
	ms := pluginMS
	if ms == 0 {
		ms = p.cfg.DefaultTimeoutMS
	}
	return time.Duration(ms) * time.Millisecond
}

func isGuarded(path string) bool {
	for _, g := range hardGuardedPaths {
		if path == g || hasPrefix(path, g+"/") {
			return true
		}
	}
	return false
}

func hasPrefix(s, prefix string) bool {
	return len(s) >= len(prefix) && s[:len(prefix)] == prefix
}

func boolPtr(b bool) *bool { return &b }

func reqWithObject(req types.Review, obj types.Object) types.Review {
	req.Object = obj
	return req
}

func ctxErrIsTimeout(err error) bool {
	type timeoutish interface{ Timeout() bool }
	if te, ok := err.(timeoutish); ok && te.Timeout() {
		return true
	}
	return false
}

// normalizeObject guarantees the container maps exist so that patch parents
// (/metadata/labels, /metadata/annotations, /spec/extra) are always objects.
func normalizeObject(o *types.Object) {
	if o.Metadata.Labels == nil {
		o.Metadata.Labels = map[string]string{}
	}
	if o.Metadata.Annotations == nil {
		o.Metadata.Annotations = map[string]string{}
	}
	if o.Spec.Extra == nil {
		o.Spec.Extra = map[string]any{}
	}
}
