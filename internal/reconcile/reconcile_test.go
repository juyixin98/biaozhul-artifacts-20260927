package reconcile

import (
	"context"
	"testing"
	"time"

	"admission/internal/admission"
	"admission/internal/plugins"
	"admission/internal/quota"
	"admission/internal/service"
	"admission/internal/storage"
	"admission/internal/types"
)

type clock struct{ t time.Time }

func newClock() *clock                   { return &clock{t: time.Unix(1_000_000, 0)} }
func (c *clock) now() time.Time          { return c.t }
func (c *clock) advance(d time.Duration) { c.t = c.t.Add(d) }

type harness struct {
	rec    *Reconciler
	store  *storage.SQLiteStore
	clk    *clock
	svc    *service.Service
	ledger *quota.MemoryLedger
}

func newHarness(t *testing.T, mutators []admission.MutatorSpec, ledger *quota.MemoryLedger,
	validators ...admission.ValidatorSpec) harness {
	t.Helper()
	store, err := storage.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })

	cfg := admission.Config{
		DefaultTimeoutMS: 250, MaxMutationPasses: 3,
		Defaults: []admission.MutatorSpec{
			{Plugin: &plugins.ReplicaDefaulter{Default: 3}, FailurePolicy: admission.FailClose},
			{Plugin: &plugins.ResourcesDefaulter{DefaultCPU: "250m", DefaultMemory: "128Mi"}, FailurePolicy: admission.FailClose},
		},
		Mutators:   mutators,
		Validators: validators,
	}
	if len(cfg.Validators) == 0 {
		cfg.Validators = []admission.ValidatorSpec{
			{Plugin: &plugins.ReplicaRangeValidator{Min: 1, Max: 10}, FailurePolicy: admission.FailClose},
			{Plugin: &plugins.ImmutableValidator{}, FailurePolicy: admission.FailClose},
		}
		if ledger != nil {
			cfg.Validators = append(cfg.Validators,
				admission.ValidatorSpec{Plugin: &plugins.QuotaValidator{Adapter: ledger}, FailurePolicy: admission.FailClose})
		}
	}
	pipe, err := admission.New(cfg, nil)
	if err != nil {
		t.Fatalf("pipeline: %v", err)
	}
	var led quota.Adapter
	if ledger != nil {
		led = ledger
	}
	svc, err := service.New(service.Deps{Pipeline: pipe, Store: store, Ledger: led,
		NewID: func() string { return "run-svc" }})
	if err != nil {
		t.Fatalf("service: %v", err)
	}
	clk := newClock()
	seq := 0
	newID := func() string { seq++; return "run-rec-" + itoa(seq) }
	rec := New(svc, store, Policy{
		MaxAttempts: 4, BaseDelay: 50 * time.Millisecond, Factor: 2, MaxDelay: time.Second,
	}, clk.now, newID)
	return harness{rec: rec, store: store, clk: clk, svc: svc, ledger: ledger}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	out := ""
	for n > 0 {
		out = string(rune('0'+n%10)) + out
		n /= 10
	}
	return out
}

func review(uid string) types.Review {
	return types.Review{UID: uid, Operation: "CREATE", Object: types.Object{
		APIVersion: "apps.example.com/v1", Kind: "Workload",
		Metadata: types.Metadata{Namespace: "ns", Name: uid},
		Spec:     types.Spec{CPU: "500m", Memory: "128Mi"},
	}}
}

// Backoff schedule: attempt n waits BaseDelay * Factor^(n-2).
func TestBackoffSchedule(t *testing.T) {
	h := newHarness(t, nil, nil)
	want := map[int]time.Duration{2: 50 * time.Millisecond, 3: 100 * time.Millisecond, 4: 200 * time.Millisecond}
	for n, d := range want {
		if got := h.rec.delayFor(n); got != d {
			t.Errorf("delayFor(%d)=%v want %v", n, got, d)
		}
	}
}

// An initially failing (compute) request is retried with backoff and then
// admitted; each attempt has a distinct run id and the audit trail keeps all.
func TestRetryThenSuccess(t *testing.T) {
	flaky := &plugins.FlakyMutator{
		Name_: "flaky", Failures: 2, FailCategory: types.CatComputeFailure,
		AllowedPaths: []string{"/spec/extra/flaky"},
	}
	h := newHarness(t, []admission.MutatorSpec{
		{Plugin: flaky, FailurePolicy: admission.FailClose},
	}, nil)
	ctx := context.Background()
	req := review("u-flaky")

	if err := h.rec.Submit(ctx, req); err != nil {
		t.Fatal(err)
	}
	// Attempt 1 fails compute; a retry is scheduled 50ms in the future.
	o1 := h.rec.Tick(ctx)
	if o1.Idle || o1.Allowed || o1.Category != types.CatComputeFailure || !o1.Retrying {
		t.Fatalf("attempt1 outcome wrong: %+v", o1)
	}

	// Before the backoff elapses the queue is idle (item not due).
	oIdle := h.rec.Tick(ctx)
	if !oIdle.Idle {
		t.Fatalf("item must not be due before backoff: %+v", oIdle)
	}

	h.clk.advance(49 * time.Millisecond)
	if !h.rec.Tick(ctx).Idle {
		t.Fatal("still not due at 49ms")
	}
	h.clk.advance(2 * time.Millisecond)

	// Attempt 2 fails again and backs off 100ms.
	o2 := h.rec.Tick(ctx)
	if o2.Allowed || o2.Attempt != 2 || !o1.Retrying || !o2.Retrying {
		t.Fatalf("attempt2 wrong: %+v", o2)
	}
	h.clk.advance(100 * time.Millisecond)

	// Attempt 3: flaky plugin recovered -> admitted, item removed.
	o3 := h.rec.Tick(ctx)
	if !o3.Allowed || o3.Attempt != 3 {
		t.Fatalf("attempt3 should succeed: %+v", o3)
	}
	if n, _ := h.store.CountRetry(ctx); n != 0 {
		t.Fatalf("queue should be drained, count=%d", n)
	}
	if flaky.Calls() != 3 {
		t.Fatalf("flaky calls=%d want 3", flaky.Calls())
	}

	// Stored verdict is the successful one; audit keeps all three attempts.
	events, _ := h.store.RecentAudit(ctx, 10)
	if len(events) != 3 {
		t.Fatalf("audit attempts=%d want 3", len(events))
	}
	stored, ok, _ := h.store.LookupUID(ctx, "u-flaky")
	if !ok || !stored.Allowed {
		t.Fatalf("stored verdict must be the final success: %+v", stored)
	}
}

// Non-retryable failures (illegal mutation, invalid input) are dropped
// immediately and never retried.
func TestNonRetryableIsTerminal(t *testing.T) {
	rogue := &plugins.RogueMutator{Name_: "rogue", Attack: "outside"}
	h := newHarness(t, []admission.MutatorSpec{
		{Plugin: rogue, FailurePolicy: admission.FailClose},
	}, nil)
	ctx := context.Background()
	if err := h.rec.Submit(ctx, review("u-rogue")); err != nil {
		t.Fatal(err)
	}
	o := h.rec.Tick(ctx)
	if o.Category != types.CatIllegalMutation || o.Retrying || o.Dropped != true {
		t.Fatalf("illegal mutation must be dropped, got %+v", o)
	}
	if !h.rec.Tick(ctx).Idle {
		t.Fatal("terminal item must not be retried")
	}
}

// Attempt cap: persistent retryable failure gives up after MaxAttempts.
func TestAttemptCap(t *testing.T) {
	// quota ledger too small: request always exhausts (retryable).
	ledger := quota.NewMemoryLedger(10, 1<<30)
	h := newHarness(t, nil, ledger)
	ctx := context.Background()
	if err := h.rec.Submit(ctx, review("u-starved")); err != nil {
		t.Fatal(err)
	}
	var attempts int
	for i := 0; i < 20; i++ {
		o := h.rec.Tick(ctx)
		if o.Idle {
			break
		}
		attempts++
		if o.Category != types.CatQuotaExhausted {
			t.Fatalf("attempt %d category=%s", attempts, o.Category)
		}
		if o.Retrying {
			h.clk.advance(time.Second)
		}
		if o.Dropped {
			break
		}
	}
	if attempts != 4 {
		t.Fatalf("attempts=%d want MaxAttempts=4", attempts)
	}
	if n, _ := h.store.CountRetry(ctx); n != 0 {
		t.Fatalf("exhausted item must be removed after cap, count=%d", n)
	}
}
