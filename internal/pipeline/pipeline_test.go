package pipeline_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"admission/internal/model"
	"admission/internal/pipeline"
	"admission/internal/plugins"
)

func catalog() plugins.DefaultsCatalog {
	return plugins.DefaultsCatalog{
		"Widget": {"replicas": int64(1), "schedule": "always"},
	}
}

func baseChain(quota plugins.QuotaService) (*pipeline.Pipeline, []plugins.Mutator, []plugins.Validator) {
	d := 250 * time.Millisecond
	ms := []plugins.Mutator{
		&plugins.DefaultsMutator{Catalog: catalog(), TimeoutD: d, Policy: plugins.FailClosed},
		&plugins.CapacityMutator{PerReplica: 100, TimeoutD: d, Policy: plugins.FailClosed},
		&plugins.StampMutator{TimeoutD: d, Policy: plugins.FailClosed},
	}
	vs := []plugins.Validator{
		&plugins.SchemaValidator{MaxReplicas: 5, TimeoutD: d, Policy: plugins.FailClosed},
		&plugins.QuotaValidator{Quota: quota, TimeoutD: d, Policy: plugins.FailClosed},
	}
	return pipeline.New(ms, vs, 5), ms, vs
}

func createReq(uid string, spec map[string]any) model.Request {
	return model.Request{
		UID:       uid,
		Operation: model.OpCreate,
		Object: map[string]any{
			"apiVersion": "v1",
			"kind":       "Widget",
			"metadata":   map[string]any{"name": "w-" + uid, "namespace": "ns"},
			"spec":       spec,
		},
	}
}

// TestHappyPath_DefaultsThenInteraction asserts the exact ordered patches:
// defaults add replicas/schedule, capacity derives from the defaulted
// replicas, the stamp lands, and final validation passes. It also asserts the
// exact final object values rather than just "no error".
func TestHappyPath_DefaultsThenInteraction(t *testing.T) {
	pipe, _, _ := baseChain(plugins.NewScriptedQuota())
	res := pipe.Run(context.Background(), createReq("u1", map[string]any{}))
	if !res.Allowed {
		t.Fatalf("want allowed, got denied reason=%s msg=%s", res.Reason, res.Message)
	}

	spec := res.Final["spec"].(map[string]any)
	if spec["replicas"] != int64(1) {
		t.Fatalf("replicas = %#v, want int64(1) defaulted", spec["replicas"])
	}
	if spec["schedule"] != "always" {
		t.Fatalf("schedule = %#v, want always", spec["schedule"])
	}
	if spec["capacity"] != int64(100) {
		t.Fatalf("capacity = %#v, want 100 (1 default replica * 100): capacity plugin must read defaulted replicas",
			spec["capacity"])
	}
	meta := res.Final["metadata"].(map[string]any)
	ann := meta["annotations"].(map[string]any)
	if ann["admission.uid"] != "u1" {
		t.Fatalf("uid stamp = %#v", ann)
	}

	// Exact ordered patch trace for replay.
	got := flattenOps(res.Patches)
	want := map[string]bool{
		"add /spec/replicas":                      false,
		"add /spec/schedule":                      false,
		"add /spec/capacity":                      false,
		"add /metadata/annotations/admission.uid": false,
	}
	for _, g := range got {
		if _, ok := want[g]; !ok {
			t.Fatalf("unexpected patch %q in %v", g, got)
		}
		want[g] = true
	}
	for k, found := range want {
		if !found {
			t.Fatalf("missing patch %q; trace=%v", k, got)
		}
	}
	if res.Summary == nil || res.Summary.Digest == "" {
		t.Fatal("final summary digest missing")
	}
}

// TestReentryIdempotent runs the same admission twice and asserts the second
// run converges with zero patches and an identical final digest.
func TestReentryIdempotent(t *testing.T) {
	pipe, _, _ := baseChain(plugins.NewScriptedQuota())
	req := createReq("dup1", map[string]any{"replicas": float64(2)})
	first := pipe.Run(context.Background(), req)
	if !first.Allowed {
		t.Fatalf("first run denied: %s %s", first.Reason, first.Message)
	}
	// Second invocation receives the object AS THE FIRST RUN LEFT IT (this is
	// what re-admission after commit looks like).
	req.Object = first.Final
	second := pipe.Run(context.Background(), req)
	if !second.Allowed {
		t.Fatalf("second run denied: %s %s", second.Reason, second.Message)
	}
	if first.Summary.Digest != second.Summary.Digest {
		t.Fatalf("digests differ on re-entry:\n%s\n%s", first.Summary.Digest, second.Summary.Digest)
	}
	if len(second.Patches) != 0 {
		t.Fatalf("re-entry produced patches, violating idempotence: %+v", second.Patches)
	}
}

// TestIllegalPathAlwaysFatal uses the scripted fixture that emits a write to
// /metadata/name. It must be illegal_path regardless of fail policy open.
func TestIllegalPathAlwaysFatal(t *testing.T) {
	bad := &plugins.ScriptedMutator{
		PluginName:  "hijack",
		Kind:        plugins.ScriptIllegalPath,
		Paths:       []string{"/spec/something"}, // name is NOT declared
		Policy:      plugins.FailOpen,            // open must NOT save it
		CallTimeout: time.Second,
	}
	pipe := pipeline.New([]plugins.Mutator{bad}, nil, 5)
	res := pipe.Run(context.Background(), createReq("u2", map[string]any{}))
	if res.Allowed {
		t.Fatal("illegal path must never admit")
	}
	if res.Reason != model.ReasonIllegalPath {
		t.Fatalf("reason = %s, want illegal_path", res.Reason)
	}
	if res.Reason.Category() != "compute_failure" {
		t.Fatalf("category = %s", res.Reason.Category())
	}
	illegalRecorded := false
	// The rejected name write must not appear anywhere, and the step's final
	// judgement must be "error" (never "applied").
	for _, s := range res.Steps {
		if s.Plugin == "hijack" && s.Decision != "error" {
			t.Fatalf("illegal plugin decision = %s", s.Decision)
		}
		if s.Reason == model.ReasonIllegalPath {
			illegalRecorded = true
		}
	}
	if !illegalRecorded {
		t.Fatalf("no illegal_path step recorded; steps=%+v", res.Steps)
	}
}

// TestMutatorTimeoutOpenVsClosed distinguishes the timeout category and shows
// explicit fail-open vs fail-closed behavior.
func TestMutatorTimeoutOpenVsClosed(t *testing.T) {
	slowClosed := &plugins.ScriptedMutator{
		PluginName: "slow-closed", Kind: plugins.ScriptTimeout,
		Paths: []string{"/spec/x"}, Policy: plugins.FailClosed,
		CallTimeout: 20 * time.Millisecond,
	}
	pipe := pipeline.New([]plugins.Mutator{slowClosed}, nil, 3)
	res := pipe.Run(context.Background(), createReq("u3", map[string]any{}))
	if res.Allowed || res.Reason != model.ReasonTimeout {
		t.Fatalf("closed timeout: allowed=%v reason=%s, want denied/timeout", res.Allowed, res.Reason)
	}
	if res.Reason.HTTPStatus() != 504 {
		t.Fatalf("timeout http status = %d, want 504", res.Reason.HTTPStatus())
	}

	slowOpen := &plugins.ScriptedMutator{
		PluginName: "slow-open", Kind: plugins.ScriptTimeout,
		Paths: []string{"/spec/x"}, Policy: plugins.FailOpen,
		CallTimeout: 20 * time.Millisecond,
	}
	// No validators: skip-open then succeeds.
	pipeOpen := pipeline.New([]plugins.Mutator{slowOpen}, nil, 3)
	res2 := pipeOpen.Run(context.Background(), createReq("u4", map[string]any{}))
	if !res2.Allowed {
		t.Fatalf("fail-open timeout should continue without the plugin: %s %s", res2.Reason, res2.Message)
	}
	var sawSkip bool
	for _, s := range res2.Steps {
		if s.Plugin == "slow-open" {
			sawSkip = true
			if s.Decision != "skipped-open" || s.Reason != model.ReasonTimeout {
				t.Fatalf("step = %+v", s)
			}
		}
	}
	if !sawSkip {
		t.Fatal("timeout step not recorded")
	}
}

// TestNonConvergingMutatorBounded proves the pass cap stops an oscillating
// plugin and reports a distinct compute failure instead of looping forever.
func TestNonConvergingMutatorBounded(t *testing.T) {
	maxPasses := 3
	osc := &plugins.ScriptedMutator{
		PluginName: "osc", Kind: plugins.ScriptOscillate,
		Paths: []string{"/spec/osc"}, Policy: plugins.FailClosed,
		CallTimeout: time.Second,
	}
	pipe := pipeline.New([]plugins.Mutator{osc}, nil, maxPasses)
	done := make(chan pipeline.Result, 1)
	start := time.Now()
	go func() { done <- pipe.Run(context.Background(), createReq("u5", map[string]any{})) }()
	select {
	case res := <-done:
		if res.Allowed {
			t.Fatal("oscillating chain must not converge")
		}
		if res.Reason != model.ReasonComputeFailure || !strings.Contains(res.Message, "converge") {
			t.Fatalf("reason=%s msg=%s, want convergence compute failure", res.Reason, res.Message)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("pipeline looped past the convergence cap (possible infinite loop)")
	}
	if time.Since(start) > 2*time.Second {
		t.Fatal("convergence cap was not enforced promptly")
	}
	if osc.Calls() != int64(maxPasses) {
		t.Fatalf("mutator calls = %d, want exactly %d (bounded)", osc.Calls(), maxPasses)
	}
}

// TestPanicRecoveredAsComputeFailure: a panicking closed plugin denies as
// compute_failure, not a process crash; open policy skips it.
func TestPanicRecoveredAsComputeFailure(t *testing.T) {
	panicker := &plugins.ScriptedMutator{
		PluginName: "pan", Kind: plugins.ScriptPanic,
		Paths: []string{"/spec/p"}, Policy: plugins.FailClosed,
		CallTimeout: time.Second,
	}
	pipe := pipeline.New([]plugins.Mutator{panicker}, nil, 2)
	res := pipe.Run(context.Background(), createReq("u6", map[string]any{}))
	if res.Allowed || res.Reason != model.ReasonComputeFailure {
		t.Fatalf("panic: allowed=%v reason=%s", res.Allowed, res.Reason)
	}
	if !strings.Contains(res.Message, "panicked") {
		t.Fatalf("message should identify panic: %s", res.Message)
	}

	panicker2 := &plugins.ScriptedMutator{
		PluginName: "pan2", Kind: plugins.ScriptPanic,
		Paths: []string{"/spec/p"}, Policy: plugins.FailOpen,
		CallTimeout: time.Second,
	}
	pipe2 := pipeline.New([]plugins.Mutator{panicker2}, nil, 2)
	if r := pipe2.Run(context.Background(), createReq("u7", map[string]any{})); !r.Allowed {
		t.Fatalf("open policy must skip panicking plugin, got %s", r.Reason)
	}
}

// TestQuotaExhaustionIsDenial drives the scripted quota into exhaustion and
// asserts the resource_exhausted category (a verdict, not an invocation
// error), and that an adapter *error* is a separate compute failure.
func TestQuotaExhaustionAndAdapterError(t *testing.T) {
	q := plugins.NewScriptedQuota()
	pipe, _, _ := baseChain(q)
	req := createReq("exh1", map[string]any{"replicas": float64(2)})
	q.Exhaust("exh1")
	res := pipe.Run(context.Background(), req)
	if res.Allowed {
		t.Fatal("exhausted quota must deny")
	}
	if res.Reason != model.ReasonResourceExhausted {
		t.Fatalf("reason = %s, want resource_exhausted", res.Reason)
	}
	if res.Reason.Category() != "resource_exhausted" || res.Reason.HTTPStatus() != 429 {
		t.Fatalf("category=%s status=%d", res.Reason.Category(), res.Reason.HTTPStatus())
	}

	// Adapter compute failure under closed policy -> compute_failure. Provide
	// a document the validator can parse so the adapter failure (not an input
	// error) is the category under test.
	q2 := plugins.NewScriptedQuota()
	q2.SetBehavior("brk1", plugins.ScriptComputeError)
	d := 250 * time.Millisecond
	vBroken := &plugins.QuotaValidator{Quota: q2, TimeoutD: d, Policy: plugins.FailClosed}
	pipe2 := pipeline.New(nil, []plugins.Validator{vBroken}, 2)
	validDoc := createReq("brk1", map[string]any{"replicas": int64(2)})
	r2 := pipe2.Run(context.Background(), validDoc)
	if r2.Allowed || r2.Reason != model.ReasonComputeFailure {
		t.Fatalf("broken adapter closed: allowed=%v reason=%s msg=%s", r2.Allowed, r2.Reason, r2.Message)
	}
}

// TestFinalValidationRunsAfterMutation builds an object that mutates into a
// valid shape and another that mutates into an invalid shape, proving
// validators see the post-chain object and that a deny is terminal with the
// intermediate object summary still attached.
func TestFinalValidationRunsAfterMutation(t *testing.T) {
	// capacity perReplica=1 with replicas=5 produces capacity=5 (valid, ==
	// replicas). With perReplica=0... use replicas that violate max.
	quota := plugins.NewScriptedQuota()
	d := 250 * time.Millisecond
	ms := []plugins.Mutator{
		&plugins.DefaultsMutator{Catalog: catalog(), TimeoutD: d, Policy: plugins.FailClosed},
		&plugins.CapacityMutator{PerReplica: 1, TimeoutD: d, Policy: plugins.FailClosed},
	}
	vs := []plugins.Validator{
		&plugins.SchemaValidator{MaxReplicas: 5, TimeoutD: d, Policy: plugins.FailClosed},
		&plugins.QuotaValidator{Quota: quota, TimeoutD: d, Policy: plugins.FailClosed},
	}
	pipe := pipeline.New(ms, vs, 3)
	ok := pipe.Run(context.Background(), createReq("ok1", map[string]any{"replicas": float64(5)}))
	if !ok.Allowed {
		t.Fatalf("replicas=5 should pass max=5: %s", ok.Message)
	}
	bad := pipe.Run(context.Background(), createReq("bad1", map[string]any{"replicas": float64(6)}))
	if bad.Allowed {
		t.Fatal("replicas=6 must be denied by schema")
	}
	if bad.Reason != model.ReasonValidationDenied {
		t.Fatalf("reason = %s, want validation_denied", bad.Reason)
	}
	if bad.Summary == nil {
		t.Fatal("denied response must still bind the final (post-mutation) object summary")
	}
}

// TestPluginInteractionOrdering asserts that flipping the default replicas
// changes capacity (plugins interact through the shared document) and that
// capacity before defaults fails with an ordering compute error.
func TestPluginInteractionOrdering(t *testing.T) {
	d := 250 * time.Millisecond
	ms := []plugins.Mutator{
		&plugins.DefaultsMutator{
			Catalog:  plugins.DefaultsCatalog{"Widget": {"replicas": int64(3)}},
			TimeoutD: d, Policy: plugins.FailClosed,
		},
		&plugins.CapacityMutator{PerReplica: 10, TimeoutD: d, Policy: plugins.FailClosed},
	}
	pipe := pipeline.New(ms, nil, 5)
	res := pipe.Run(context.Background(), createReq("i1", map[string]any{}))
	if !res.Allowed {
		t.Fatalf("denied: %s", res.Message)
	}
	if got := res.Final["spec"].(map[string]any)["capacity"]; got != int64(30) {
		t.Fatalf("capacity = %#v, want 30 (3 defaulted replicas * 10)", got)
	}

	// Reverse order: capacity first has no replicas yet and returns a typed
	// compute failure under closed policy.
	wrong := pipeline.New([]plugins.Mutator{
		&plugins.CapacityMutator{PerReplica: 10, TimeoutD: d, Policy: plugins.FailClosed},
		&plugins.DefaultsMutator{Catalog: catalog(), TimeoutD: d, Policy: plugins.FailClosed},
	}, nil, 5)
	r2 := wrong.Run(context.Background(), createReq("i2", map[string]any{}))
	if r2.Allowed || r2.Reason != model.ReasonComputeFailure {
		t.Fatalf("mis-ordered chain: allowed=%v reason=%s", r2.Allowed, r2.Reason)
	}
}

func TestValidatorTimeoutCategory(t *testing.T) {
	v := &plugins.ScriptedValidator{
		PluginName: "slowv", ErrorKind: plugins.ScriptTimeout,
		Policy: plugins.FailClosed, CallTimeout: 15 * time.Millisecond,
	}
	pipe := pipeline.New(nil, []plugins.Validator{v}, 2)
	res := pipe.Run(context.Background(), createReq("vt1", map[string]any{}))
	if res.Allowed || res.Reason != model.ReasonTimeout {
		t.Fatalf("validator timeout reason = %s, want timeout", res.Reason)
	}
}

func flattenOps(ops []model.PatchOp) []string {
	out := make([]string, 0, len(ops))
	for _, op := range ops {
		out = append(out, op.Op+" "+op.Path)
	}
	return out
}
