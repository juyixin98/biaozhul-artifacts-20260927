package service

import (
	"context"
	"testing"

	"admission/internal/admission"
	"admission/internal/plugins"
	"admission/internal/quota"
	"admission/internal/storage"
	"admission/internal/types"
)

func newTestService(t *testing.T, ledgerCap bool) (*Service, *storage.SQLiteStore, *quota.MemoryLedger) {
	t.Helper()
	store, err := storage.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })

	var ledger *quota.MemoryLedger
	var qv *plugins.QuotaValidator
	if ledgerCap {
		ledger = quota.NewMemoryLedger(1000, 1<<30)
		qv = &plugins.QuotaValidator{Adapter: ledger}
	}
	cfg := admission.Config{
		DefaultTimeoutMS:  250,
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
	if qv != nil {
		cfg.Validators = append(cfg.Validators,
			admission.ValidatorSpec{Plugin: qv, FailurePolicy: admission.FailClose})
	}
	pipe, err := admission.New(cfg, nil)
	if err != nil {
		t.Fatalf("pipeline: %v", err)
	}
	svc, err := New(Deps{Pipeline: pipe, Store: store, Ledger: ledger})
	if err != nil {
		t.Fatalf("service: %v", err)
	}
	return svc, store, ledger
}

func workload(uid string) types.Review {
	return types.Review{
		UID: uid, Operation: "CREATE",
		Object: types.Object{
			APIVersion: "apps.example.com/v1", Kind: "Workload",
			Metadata: types.Metadata{Namespace: "team-a", Name: "w", Labels: map[string]string{"team": "pay"}},
			Spec:     types.Spec{CPU: "500m", Memory: "128Mi"},
		},
	}
}

// Successful CREATE: final summary binds to the final object; capacity is
// reserved once; the immutable flag is set by the service post-admission.
func TestAdmitSuccessBindsSummaryAndReserves(t *testing.T) {
	svc, store, ledger := newTestService(t, true)
	ctx := context.Background()
	req := workload("u-success")
	before := req.Object.Fingerprint()

	resp := svc.Admit(ctx, "run-1", req, 1)
	if !resp.Allowed {
		t.Fatalf("expected allowed: cat=%s msg=%s", resp.FailureCategory, resp.Message)
	}
	if resp.Object.Spec.Replicas == nil || *resp.Object.Spec.Replicas != 3 {
		t.Fatalf("default replicas missing: %+v", resp.Object.Spec)
	}
	if !resp.Object.Spec.Immutable {
		t.Fatal("admitted object must be flagged immutable by the service")
	}
	if resp.Final.Fingerprint != resp.Object.Fingerprint() {
		t.Fatal("final summary fingerprint must bind to final object")
	}
	if resp.Final.Replicas != 3 || resp.Final.CPU != "500m" {
		t.Fatalf("summary scalars wrong: %+v", resp.Final)
	}

	// Ledger booked exactly 500m.
	cpu, _ := ledger.Used()
	if cpu != 500 {
		t.Fatalf("ledger used cpu=%d want 500", cpu)
	}

	// Audit row binds UID -> final summary, before/after hashes, steps.
	events, err := store.RecentAudit(ctx, 5)
	if err != nil || len(events) != 1 {
		t.Fatalf("audit events=%d err=%v", len(events), err)
	}
	ev := events[0]
	if ev.UID != "u-success" || !ev.Terminal || !ev.Allowed {
		t.Fatalf("audit event wrong: %+v", ev)
	}
	if ev.BeforeHash != before {
		t.Fatalf("before hash mismatch: %s != %s", ev.BeforeHash, before)
	}
	if ev.AfterHash != resp.Object.Fingerprint() {
		t.Fatalf("after hash must bind final object")
	}
	if len(ev.Steps) == 0 {
		t.Fatal("audit must retain every step")
	}
}

// Duplicate call: same UID returns the stored verdict with Replayed=true; the
// chain is not re-run and no additional capacity is booked.
func TestDuplicateCallReplays(t *testing.T) {
	svc, store, ledger := newTestService(t, true)
	ctx := context.Background()
	req := workload("u-dup")

	first := svc.Admit(ctx, "run-first", req, 1)
	if !first.Allowed {
		t.Fatalf("first call: %s %s", first.FailureCategory, first.Message)
	}
	cpu1, _ := ledger.Used()

	// Mutate the caller's object; the duplicate must ignore it.
	req2 := req
	n := 9
	req2.Object.Spec.Replicas = &n
	second := svc.Admit(ctx, "run-second", req2, 2)
	if !second.Replayed {
		t.Fatal("duplicate UID must be marked Replayed")
	}
	if !second.Allowed || second.Final.Fingerprint != first.Final.Fingerprint {
		t.Fatalf("replayed verdict differs:\n%+v\n%+v", first.Final, second.Final)
	}
	if second.Object.Spec.Replicas == nil || *second.Object.Spec.Replicas != 3 {
		t.Fatalf("replay must return stored object, got replicas=%v", second.Object.Spec.Replicas)
	}
	cpu2, _ := ledger.Used()
	if cpu1 != cpu2 {
		t.Fatalf("duplicate call booked extra capacity: %d -> %d", cpu1, cpu2)
	}

	// The replay audit event must bind to the CREATE operation (not the kind)
	// and mark itself as a replay.
	events, err := store.RecentAudit(ctx, 5)
	if err != nil {
		t.Fatal(err)
	}
	var replayEv *types.AuditEvent
	for i := range events {
		if events[i].Replayed {
			replayEv = &events[i]
		}
	}
	if replayEv == nil {
		t.Fatal("expected a replayed audit event")
	}
	if replayEv.Operation != "CREATE" || replayEv.Kind != "Workload" {
		t.Fatalf("replay audit op=%q kind=%q", replayEv.Operation, replayEv.Kind)
	}
	if replayEv.Message == "" {
		t.Fatal("replay audit must explain why the chain was not re-run")
	}
}

// Quota exhaustion at reserve time yields the QuotaExhausted category.
func TestQuotaExhaustionCategory(t *testing.T) {
	svc, _, ledger := newTestService(t, true)
	ctx := context.Background()
	// Pre-consume almost all capacity.
	if err := ledger.Reserve("seed", 700, 1); err != nil {
		t.Fatal(err)
	}
	req := workload("u-big") // needs 500m cpu; only 300m remains
	resp := svc.Admit(ctx, "run-big", req, 1)
	if resp.Allowed {
		t.Fatal("oversize request must not be allowed")
	}
	if resp.FailureCategory != types.CatQuotaExhausted {
		t.Fatalf("category=%s want QuotaExhausted", resp.FailureCategory)
	}
	if resp.FailurePhase != types.PhaseValidating {
		t.Fatalf("phase=%s want validating", resp.FailurePhase)
	}
}

// Dry run never books capacity and is not stored as the UID verdict.
func TestDryRunNoSideEffects(t *testing.T) {
	svc, store, ledger := newTestService(t, true)
	ctx := context.Background()
	req := workload("u-dry")
	req.DryRun = true
	resp := svc.Admit(ctx, "run-dry", req, 1)
	if !resp.Allowed {
		t.Fatalf("dry run: %s", resp.Message)
	}
	if cpu, _ := ledger.Used(); cpu != 0 {
		t.Fatalf("dry run booked capacity: %d", cpu)
	}
	if resp.Object.Spec.Immutable {
		t.Fatal("dry run must not flip the immutable flag")
	}
	if _, ok, _ := store.LookupUID(ctx, "u-dry"); ok {
		t.Fatal("dry run must not be stored as the UID verdict")
	}
	// A subsequent non-dry call with the same UID must run normally.
	req.DryRun = false
	real := svc.Admit(ctx, "run-real", req, 1)
	if !real.Allowed || real.Replayed {
		t.Fatalf("post-dry-run call should execute: allowed=%v replayed=%v", real.Allowed, real.Replayed)
	}
}

// Invalid input is terminal, audited and distinguishable from compute errors.
func TestInvalidInputIsDistinctAndStored(t *testing.T) {
	svc, store, _ := newTestService(t, false)
	ctx := context.Background()
	req := workload("u-bad")
	req.Object.Spec.CPU = "not-a-quantity" // mutator ReservedResources rejects it
	resp := svc.Admit(ctx, "run-bad", req, 1)
	if resp.Allowed {
		t.Fatal("bad quantity must fail")
	}
	if resp.FailureCategory != types.CatInvalidInput {
		t.Fatalf("category=%s want InvalidInput", resp.FailureCategory)
	}
	if Retryable(resp) {
		t.Fatal("invalid input must not be retryable")
	}
	// Stored so duplicates are observable and consistent.
	if _, ok, _ := store.LookupUID(ctx, "u-bad"); !ok {
		t.Fatal("invalid-input verdict should be stored")
	}
}
