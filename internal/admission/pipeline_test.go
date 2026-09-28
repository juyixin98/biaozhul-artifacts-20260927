package admission_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"admission/internal/admission"
	"admission/internal/plugins"
	"admission/internal/types"
)

// recLogger captures entries for assertions on reasons and intermediate state.
type recLogger struct{ entries []admission.LogEntry }

func (r *recLogger) Log(e admission.LogEntry) { r.entries = append(r.entries, e) }

func intPtr(n int) *int { return &n }

func baseWorkload() types.Object {
	return types.Object{
		APIVersion: "apps.example.com/v1",
		Kind:       "Workload",
		Metadata:   types.Metadata{Namespace: "team-a", Name: "w1", Labels: map[string]string{"team": "payments"}},
		Spec:       types.Spec{CPU: "500m", Memory: "128Mi"},
	}
}

func newTestPipeline(t *testing.T, logger admission.RunLogger, cfg admission.Config) *admission.Pipeline {
	t.Helper()
	p, err := admission.New(cfg, logger)
	if err != nil {
		t.Fatalf("New pipeline: %v", err)
	}
	return p
}

func standardConfig(timeoutMS int) admission.Config {
	return admission.Config{
		DefaultTimeoutMS:  timeoutMS,
		MaxMutationPasses: 3,
		Defaults: []admission.MutatorSpec{
			{Plugin: &plugins.ReplicaDefaulter{Default: 3}, FailurePolicy: admission.FailClose},
			{Plugin: &plugins.ResourcesDefaulter{DefaultCPU: "250m", DefaultMemory: "128Mi"}, FailurePolicy: admission.FailClose},
		},
		Mutators: []admission.MutatorSpec{
			{Plugin: &plugins.ReservedResources{}, FailurePolicy: admission.FailClose},
			{Plugin: &plugins.LabelSync{}, FailurePolicy: admission.FailClose},
		},
		Validators: []admission.ValidatorSpec{
			{Plugin: &plugins.ReplicaRangeValidator{Min: 1, Max: 10}, FailurePolicy: admission.FailClose},
			{Plugin: &plugins.ImmutableValidator{}, FailurePolicy: admission.FailClose},
		},
	}
}

// 1) Defaults fill missing values and the final object is exactly as expected,
// including the fingerprints between steps.
func TestDefaultsFillMissingValues(t *testing.T) {
	logger := &recLogger{}
	pipe := newTestPipeline(t, logger, standardConfig(250))

	obj := baseWorkload() // no replicas
	obj.Spec.CPU = ""     // force resource defaulting too
	obj.Spec.Memory = ""
	before := obj.Fingerprint()

	res := pipe.Run(context.Background(), "run-defaults", types.Review{
		UID: "u-defaults", Operation: "CREATE", Object: obj,
	})
	if res.Err != nil {
		t.Fatalf("unexpected error: %v", res.Err)
	}
	if !res.Allowed {
		t.Fatalf("expected allowed, denied: %s", res.Deny)
	}
	if res.Object.Spec.Replicas == nil || *res.Object.Spec.Replicas != 3 {
		t.Fatalf("replicas default wrong: %v", res.Object.Spec.Replicas)
	}
	if res.Object.Spec.CPU != "250m" || res.Object.Spec.Memory != "128Mi" {
		t.Fatalf("resources default wrong: cpu=%q mem=%q", res.Object.Spec.CPU, res.Object.Spec.Memory)
	}
	if before == res.Object.Fingerprint() {
		t.Fatal("object fingerprint did not change after defaulting")
	}

	// Exactly two default steps plus the chain steps; assert the replica patch.
	var replicaStep *types.Step
	for i := range res.Steps {
		s := &res.Steps[i]
		if s.Plugin == "defaults.replicas" {
			replicaStep = s
		}
	}
	if replicaStep == nil || !replicaStep.Mutated || len(replicaStep.Patch) != 1 {
		t.Fatalf("missing/incorrect replica default step: %+v", replicaStep)
	}
	if replicaStep.Patch[0].Path != "/spec/replicas" || toFloat(replicaStep.Patch[0].Value) != 3 {
		t.Fatalf("replica patch wrong: %+v", replicaStep.Patch[0])
	}
	if replicaStep.BeforeHash == replicaStep.AfterHash {
		t.Fatal("replica step before/after hash must differ")
	}
}

// 2) Mutually influencing plugins: ReservedResources projects cpu ->
// /spec/extra/reservedCPU; LabelSync (ordered after) consumes the projection
// and the team label in the same chain; convergence takes at most one extra
// pass and the final annotations are exact.
func TestMutuallyInfluencingPlugins(t *testing.T) {
	logger := &recLogger{}
	pipe := newTestPipeline(t, logger, standardConfig(250))

	res := pipe.Run(context.Background(), "run-interact", types.Review{
		UID: "u-interact", Operation: "CREATE", Object: baseWorkload(),
	})
	if res.Err != nil {
		t.Fatalf("unexpected error: %v", res.Err)
	}
	if !res.Allowed {
		t.Fatalf("denied: %s", res.Deny)
	}

	// Projection landed.
	if got := toFloat(res.Object.Spec.Extra["reservedCPU"]); got != 500 {
		t.Fatalf("reservedCPU projection=%v want 500", res.Object.Spec.Extra)
	}
	ann := res.Object.Metadata.Annotations
	if ann["admission.example.com/team"] != "payments" {
		t.Fatalf("team annotation wrong: %v", ann)
	}
	if ann["admission.example.com/reserved-cpu-milli"] != "500" {
		t.Fatalf("reserved-cpu annotation wrong: %v", ann)
	}

	// Trace the interaction in the recorded steps: on pass 1 LabelSync runs
	// before the projection exists in execution order only relative to the same
	// pass ordering... here reserved-resources precedes label-sync, so the
	// annotation patch must appear in pass 1 and pass 2 must be a no-op.
	var labelPatches int
	var passTwoMutation bool
	for _, s := range res.Steps {
		if s.Plugin == "mutators.label-sync" && s.Mutated {
			labelPatches++
			found := false
			for _, op := range s.Patch {
				if strings.Contains(op.Path, "reserved-cpu-milli") {
					found = true
				}
			}
			if !found {
				t.Fatalf("label-sync patch missing reserved-cpu op: %+v", s.Patch)
			}
		}
		if s.Pass == 2 && s.Mutated {
			passTwoMutation = true
		}
	}
	if labelPatches != 1 {
		t.Fatalf("expected exactly one mutating label-sync step, got %d", labelPatches)
	}
	if passTwoMutation {
		t.Fatal("chain converged incorrectly: pass 2 still mutated")
	}
}

// 3) Re-entrant call on an already-defaulted object must be a no-op: zero
// mutations, same fingerprint — the idempotency contract.
func TestReentrantMutationIsIdempotent(t *testing.T) {
	pipe := newTestPipeline(t, &recLogger{}, standardConfig(250))
	req := types.Review{UID: "u-idem", Operation: "CREATE", Object: baseWorkload()}

	first := pipe.Run(context.Background(), "run-idem-1", req)
	if first.Err != nil || !first.Allowed {
		t.Fatalf("first run failed: err=%v deny=%s", first.Err, first.Deny)
	}
	// Run again against the already transformed object.
	second := pipe.Run(context.Background(), "run-idem-2", types.Review{
		UID: "u-idem-2", Operation: "CREATE", Object: first.Object,
	})
	if second.Err != nil {
		t.Fatalf("second run errored: %v", second.Err)
	}
	for _, s := range second.Steps {
		if s.Mutated {
			t.Fatalf("re-entrant step %s/%s mutated again: %+v", s.Phase, s.Plugin, s.Patch)
		}
	}
	if first.Object.Fingerprint() != second.Object.Fingerprint() {
		t.Fatal("re-entrant run changed a converged object")
	}
}

// 4) Illegal path modifications are refused with IllegalMutation, fail-close
// regardless of plugin policy, and the object is returned unchanged (no
// partial transform is committed).
func TestIllegalPathOutsidePrefixes(t *testing.T) {
	cfg := standardConfig(250)
	cfg.Mutators = append(cfg.Mutators, admission.MutatorSpec{
		Plugin:        &plugins.RogueMutator{Name_: "rogue.outside", Attack: "outside"},
		FailurePolicy: admission.FailOpen, // even fail-open cannot pardon path violations
	})
	pipe := newTestPipeline(t, &recLogger{}, cfg)

	obj := baseWorkload()
	before := obj.Fingerprint()
	res := pipe.Run(context.Background(), "run-rogue", types.Review{
		UID: "u-rogue", Operation: "CREATE", Object: obj,
	})
	if res.Err == nil {
		t.Fatal("expected illegal mutation error")
	}
	if res.Err.Category != types.CatIllegalMutation {
		t.Fatalf("category=%s want IllegalMutation", res.Err.Category)
	}
	if res.Err.Plugin != "rogue.outside" {
		t.Fatalf("failed plugin=%s", res.Err.Plugin)
	}
	if !strings.Contains(res.Err.Error(), "/spec/replicas") {
		t.Fatalf("error should name the illegal path: %v", res.Err)
	}
	// No partial transform: pipeline returns the original object.
	if res.Object.Fingerprint() != before {
		t.Fatal("failed run must not commit any mutation")
	}
}

func TestHardGuardedImmutablePath(t *testing.T) {
	cfg := standardConfig(250)
	cfg.Mutators = append(cfg.Mutators, admission.MutatorSpec{
		Plugin: &plugins.RogueMutator{Name_: "rogue.immutable", Attack: "immutable"},
		// The rogue does not even declare the prefix; but even a plugin that
		// declared /spec could not write the guard.
		FailurePolicy: admission.FailClose,
	})
	pipe := newTestPipeline(t, &recLogger{}, cfg)

	res := pipe.Run(context.Background(), "run-guard", types.Review{
		UID: "u-guard", Operation: "CREATE", Object: baseWorkload(),
	})
	if res.Err == nil || res.Err.Category != types.CatIllegalMutation {
		t.Fatalf("expected IllegalMutation, got %+v", res.Err)
	}
	if !strings.Contains(res.Err.Message, "hard-guarded") {
		t.Fatalf("expected hard-guard reason, got %s", res.Err.Message)
	}
}

// 5) Timeout is distinguishable: a slow mutator under a tight deadline yields
// CatTimeout with the Timeout flag and elapsed timing.
func TestTimeoutMutator(t *testing.T) {
	cfg := standardConfig(250)
	cfg.Mutators = append(cfg.Mutators, admission.MutatorSpec{
		Plugin: &plugins.DelayPlugin{
			Name_:        "slow.mutator",
			Delay:        300 * time.Millisecond,
			AllowedPaths: []string{"/spec/extra/delay"},
		},
		TimeoutMS:     30,
		FailurePolicy: admission.FailClose,
	})
	pipe := newTestPipeline(t, &recLogger{}, cfg)

	start := time.Now()
	res := pipe.Run(context.Background(), "run-timeout", types.Review{
		UID: "u-timeout", Operation: "CREATE", Object: baseWorkload(),
	})
	if time.Since(start) > 250*time.Millisecond {
		t.Fatal("pipeline did not enforce deadline promptly")
	}
	if res.Err == nil || res.Err.Category != types.CatTimeout {
		t.Fatalf("expected Timeout, got %+v", res.Err)
	}
	if !res.Err.Timeout {
		t.Fatal("Timeout cause flag must be set")
	}
	if res.Err.Plugin != "slow.mutator" {
		t.Fatalf("plugin=%s", res.Err.Plugin)
	}
}

// 6) Fail-open vs fail-close are explicit and observable on the same failing
// plugin.
func TestFailurePolicyOpenVsClose(t *testing.T) {
	mk := func(policy admission.FailurePolicy) (*admission.Pipeline, *recLogger) {
		cfg := standardConfig(250)
		cfg.Mutators = append(cfg.Mutators, admission.MutatorSpec{
			Plugin: &plugins.FlakyMutator{
				Name_:        "flaky.compute",
				Failures:     1,
				FailCategory: types.CatComputeFailure,
				AllowedPaths: []string{"/spec/extra/flaky"},
			},
			FailurePolicy: policy,
		})
		logger := &recLogger{}
		return newTestPipeline(t, logger, cfg), logger
	}

	closePipe, _ := mk(admission.FailClose)
	cres := closePipe.Run(context.Background(), "run-close", types.Review{
		UID: "u-close", Operation: "CREATE", Object: baseWorkload(),
	})
	if cres.Err == nil || cres.Err.Category != types.CatComputeFailure {
		t.Fatalf("fail-close should abort with ComputeFailure, got %+v", cres.Err)
	}

	openPipe, openLog := mk(admission.FailOpen)
	ores := openPipe.Run(context.Background(), "run-open", types.Review{
		UID: "u-open", Operation: "CREATE", Object: baseWorkload(),
	})
	if ores.Err != nil || !ores.Allowed {
		t.Fatalf("fail-open should allow, err=%v deny=%s", ores.Err, ores.Deny)
	}
	var tolerated *types.Step
	for i := range ores.Steps {
		if ores.Steps[i].Plugin == "flaky.compute" && ores.Steps[i].Tolerated {
			tolerated = &ores.Steps[i]
		}
	}
	if tolerated == nil || !tolerated.Tolerated || tolerated.Category != types.CatComputeFailure {
		t.Fatalf("expected tolerated step with category, got %+v", tolerated)
	}
	sawToleration := false
	for _, e := range openLog.entries {
		if strings.Contains(e.Message, "tolerated (FailOpen)") {
			sawToleration = true
		}
	}
	if !sawToleration {
		t.Fatal("replay log must record the admission.FailOpen reason")
	}
}

// 7) An oscillating plugin must be terminated as a compute failure after the
// bounded number of passes — never an infinite loop.
func TestOscillationBounded(t *testing.T) {
	cfg := admission.Config{
		DefaultTimeoutMS:  250,
		MaxMutationPasses: 3,
		Defaults:          []admission.MutatorSpec{},
		Mutators: []admission.MutatorSpec{
			{Plugin: &plugins.OscillatingMutator{}, FailurePolicy: admission.FailClose},
		},
		Validators: []admission.ValidatorSpec{
			{Plugin: &plugins.ReplicaRangeValidator{Min: 1, Max: 10}, FailurePolicy: admission.FailClose},
		},
	}
	osc := &plugins.OscillatingMutator{}
	cfg.Mutators[0].Plugin = osc
	pipe := newTestPipeline(t, &recLogger{}, cfg)

	done := make(chan admission.RunResult, 1)
	go func() {
		done <- pipe.Run(context.Background(), "run-flap", types.Review{
			UID: "u-flap", Operation: "CREATE", Object: baseWorkload(),
		})
	}()
	select {
	case res := <-done:
		if res.Err == nil || res.Err.Category != types.CatComputeFailure {
			t.Fatalf("expected compute failure on non-convergence, got %+v", res.Err)
		}
		if !strings.Contains(res.Err.Message, "converge") {
			t.Fatalf("message should explain non-convergence: %s", res.Err.Message)
		}
		if osc.Calls() != 3 {
			t.Fatalf("oscillating plugin calls=%d want exactly MaxMutationPasses=3", osc.Calls())
		}
	case <-time.After(2 * time.Second):
		t.Fatal("pipeline looped without converging")
	}
}

// 8) Clean policy denial is distinct from every error category.
func TestPolicyDenialHasNoCategory(t *testing.T) {
	pipe := newTestPipeline(t, &recLogger{}, standardConfig(250))
	obj := baseWorkload()
	n := 25
	obj.Spec.Replicas = &n
	res := pipe.Run(context.Background(), "run-deny", types.Review{
		UID: "u-deny", Operation: "CREATE", Object: obj,
	})
	if res.Err != nil {
		t.Fatalf("denial must not be an error: %v", res.Err)
	}
	if res.Allowed || !strings.Contains(res.Deny, "outside allowed range") {
		t.Fatalf("expected range denial, got allowed=%v deny=%q", res.Allowed, res.Deny)
	}
}

// 9) Immutable-field conflict on UPDATE is a StateConflict, not a denial.
func TestImmutableStateConflict(t *testing.T) {
	pipe := newTestPipeline(t, &recLogger{}, standardConfig(250))
	old := baseWorkload()
	old.Spec.Replicas = intPtr(3)
	old.Spec.Immutable = true
	cur := old
	cur.Spec.CPU = "900m"
	res := pipe.Run(context.Background(), "run-conflict", types.Review{
		UID: "u-conflict", Operation: "UPDATE", Object: cur, OldObject: &old,
	})
	if res.Err == nil || res.Err.Category != types.CatStateConflict {
		t.Fatalf("expected StateConflict, got %+v", res.Err)
	}
}

func toFloat(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	}
	return -1
}
