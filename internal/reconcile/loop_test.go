package reconcile_test

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"fieldmerge/internal/adapter"
	"fieldmerge/internal/apperr"
	"fieldmerge/internal/log"
	"fieldmerge/internal/reconcile"
	"fieldmerge/internal/store"
)

func newDeps(t *testing.T) (*store.Store, *adapter.FileAdapter, func()) {
	t.Helper()
	dir := t.TempDir()
	st, err := store.Open(filepath.Join(dir, "db.sqlite"), store.Limits{})
	if err != nil {
		t.Fatal(err)
	}
	file := &adapter.FileAdapter{RootDir: filepath.Join(dir, "applied")}
	return st, file, func() { _ = st.Close() }
}

func seed(t *testing.T, st *store.Store, kind, name, manager, cfg string) {
	t.Helper()
	if _, err := st.Apply(context.Background(), store.ApplyRequest{
		Kind: kind, Name: name, Manager: manager,
		Config: []byte(cfg), RunID: "reconcile-test",
	}); err != nil {
		t.Fatalf("seed: %v", err)
	}
}

func logger() *logx.Logger { return logx.New(nil, "reconcile-test") }

func waitFor(t *testing.T, cond func() bool, timeout time.Duration, msg string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("timed out waiting: " + msg)
}

func TestReconcile_RetryThenRecover(t *testing.T) {
	st, file, cleanup := newDeps(t)
	defer cleanup()
	seed(t, st, "widget", "w1", "net", `{"image":"v1"}`)

	fault := &adapter.FaultAdapter{
		Inner: file,
		Cfg: adapter.FaultConfig{
			FailN:  map[string]int{"widget/w1": 2},
			Reason: "downstream_5xx",
		},
	}
	cfg := reconcile.DefaultConfig()
	cfg.BaseBackoff = 10 * time.Millisecond
	cfg.MaxBackoff = 50 * time.Millisecond
	cfg.DuePollEvery = 20 * time.Millisecond
	cfg.QueueSize = 4
	loop := reconcile.New(st, fault, cfg, logger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	loop.Start(ctx)

	if err := loop.Enqueue("widget", "w1"); err != nil {
		t.Fatal(err)
	}

	waitFor(t, func() bool {
		r, _ := st.Get(ctx, "widget", "w1")
		return r != nil && r.Status == "synced"
	}, 3*time.Second, "resource reaches synced after retries")

	calls := fault.Calls()
	if len(calls) < 3 {
		t.Fatalf("expected initial + 2 failing attempts before success, got %d calls", len(calls))
	}

	// The written downstream artifact reflects the same revision as storage.
	body, err := file.ReadBack("widget", "w1")
	if err != nil {
		t.Fatalf("adapter never wrote artifact: %v", err)
	}
	if !strings.Contains(string(body), `"revision": 1`) || !strings.Contains(string(body), `"image": "v1"`) {
		t.Fatalf("artifact missing merged state:\n%s", body)
	}

	r, _ := st.Get(ctx, "widget", "w1")
	if r.LastError != "" {
		t.Fatalf("recovered resource must clear last_error, got %q", r.LastError)
	}
	cancel()
	loop.Stop()
}

func TestReconcile_PermanentFailureCapsAttempts(t *testing.T) {
	st, file, cleanup := newDeps(t)
	defer cleanup()
	seed(t, st, "widget", "dead", "net", `{"image":"v1"}`)

	fault := &adapter.FaultAdapter{
		Inner: file,
		Cfg: adapter.FaultConfig{
			FailN:     map[string]int{"widget/dead": 1 << 30},
			Permanent: true,
			Reason:    "schema_rejected",
		},
	}
	cfg := reconcile.DefaultConfig()
	cfg.BaseBackoff = 5 * time.Millisecond
	loop := reconcile.New(st, fault, cfg, logger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	loop.Start(ctx)
	if err := loop.Enqueue("widget", "dead"); err != nil {
		t.Fatal(err)
	}

	waitFor(t, func() bool {
		r, _ := st.Get(ctx, "widget", "dead")
		return r != nil && r.Status == "failed"
	}, 2*time.Second, "permanent failure marks resource failed")

	r, _ := st.Get(ctx, "widget", "dead")
	if r.Attempts != 1 {
		t.Fatalf("non-retryable failure must not retry, attempts=%d", r.Attempts)
	}
	if !strings.Contains(r.LastError, "fatal:schema_rejected") {
		t.Fatalf("last error must be classified fatal+reason, got %q", r.LastError)
	}
	if _, err := file.ReadBack("widget", "dead"); err == nil {
		t.Fatal("failed reconcile must not leave an applied artifact")
	}
	cancel()
	loop.Stop()
}

func TestReconcile_RetryBudgetExhausted(t *testing.T) {
	st, file, cleanup := newDeps(t)
	defer cleanup()
	seed(t, st, "widget", "flap", "net", `{"image":"v1"}`)

	fault := &adapter.FaultAdapter{
		Inner: file,
		Cfg: adapter.FaultConfig{
			FailN: map[string]int{"widget/flap": 1 << 30}, // always retryable failure
		},
	}
	cfg := reconcile.DefaultConfig()
	cfg.MaxAttempts = 3
	cfg.BaseBackoff = 5 * time.Millisecond
	cfg.MaxBackoff = 10 * time.Millisecond
	cfg.DuePollEvery = 10 * time.Millisecond
	loop := reconcile.New(st, fault, cfg, logger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	loop.Start(ctx)
	if err := loop.Enqueue("widget", "flap"); err != nil {
		t.Fatal(err)
	}

	waitFor(t, func() bool {
		r, _ := st.Get(ctx, "widget", "flap")
		return r != nil && r.Attempts >= cfg.MaxAttempts
	}, 3*time.Second, "retry budget exhausted -> failed")
	r, _ := st.Get(ctx, "widget", "flap")
	if r.Status != "failed" || r.Attempts != cfg.MaxAttempts {
		t.Fatalf("status=%s attempts = %d, want failed/%d", r.Status, r.Attempts, cfg.MaxAttempts)
	}
	cancel()
	loop.Stop()
}

func TestReconcile_QueueBackpressureIsTyped(t *testing.T) {
	st, file, cleanup := newDeps(t)
	defer cleanup()

	// Block the single worker so the bounded queue fills deterministically.
	gate := make(chan struct{})
	blocking := blockingAdapter{gate: gate}
	cfg := reconcile.DefaultConfig()
	cfg.Workers = 1
	cfg.QueueSize = 2
	loop := reconcile.New(st, &blocking, cfg, logger())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	loop.Start(ctx)

	seed(t, st, "widget", "a", "net", `{"x":1}`)
	seed(t, st, "widget", "b", "net", `{"x":1}`)
	seed(t, st, "widget", "c", "net", `{"x":1}`)
	seed(t, st, "widget", "d", "net", `{"x":1}`)
	_ = loop.Enqueue("widget", "a")
	_ = loop.Enqueue("widget", "b")
	_ = loop.Enqueue("widget", "c")
	err := loop.Enqueue("widget", "d")
	ae, ok := apperr.As(err)
	if !ok || ae.Category != apperr.ResourceExhausted || ae.Code != "queue_full" {
		t.Fatalf("want resource_exhausted/queue_full, got %v", err)
	}
	close(gate)
	_ = file
	cancel()
	loop.Stop()
}

type blockingAdapter struct{ gate chan struct{} }

func (b *blockingAdapter) Name() string { return "blocking" }
func (b *blockingAdapter) Apply(ctx context.Context, _ adapter.DesiredState) adapter.Outcome {
	select {
	case <-b.gate:
		return adapter.Outcome{Synced: true}
	case <-ctx.Done():
		return adapter.Outcome{Retryable: true, Reason: "ctx_done"}
	}
}
