package coordinator_test

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"admission/internal/coordinator"
	"admission/internal/model"
	"admission/internal/pipeline"
	"admission/internal/plugins"
	"admission/internal/storage"
)

func jsonString(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func jsonUnmarshal(s string, v any) error {
	return json.Unmarshal([]byte(s), v)
}

type fixture struct {
	store *storage.Store
	q     *storage.SQLQuota
	coord *coordinator.Coordinator
}

func newFixture(t *testing.T, limits map[string]int64) *fixture {
	t.Helper()
	store, err := storage.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })

	q := storage.NewSQLQuota(store, limits)
	d := time.Second
	mutators := []plugins.Mutator{
		&plugins.DefaultsMutator{
			Catalog:  plugins.DefaultsCatalog{"Widget": {"replicas": int64(1), "schedule": "always"}},
			TimeoutD: d, Policy: plugins.FailClosed,
		},
		&plugins.CapacityMutator{PerReplica: 100, TimeoutD: d, Policy: plugins.FailClosed},
		&plugins.StampMutator{TimeoutD: d, Policy: plugins.FailClosed},
	}
	validators := []plugins.Validator{
		&plugins.SchemaValidator{MaxReplicas: 5, TimeoutD: d, Policy: plugins.FailClosed},
		&plugins.QuotaValidator{Quota: q, TimeoutD: d, Policy: plugins.FailClosed},
	}
	pipe := pipeline.New(mutators, validators, 5)
	return &fixture{store: store, q: q, coord: coordinator.New(store, pipe, nil)}
}

func req(uid string, spec map[string]any) model.Request {
	return model.Request{
		UID: uid, Operation: model.OpCreate,
		Object: map[string]any{
			"apiVersion": "v1", "kind": "Widget",
			"metadata": map[string]any{"name": "w-" + uid, "namespace": "ns"},
			"spec":     spec,
		},
	}
}

func TestAdmit_HappyPathCommitsAndBindsSummary(t *testing.T) {
	f := newFixture(t, map[string]int64{"Widget": 7})
	r, err := f.coord.Admit(context.Background(), req("a1", map[string]any{}))
	if err != nil {
		t.Fatal(err)
	}
	if !r.Stored && r.Response.Decision != model.DecisionAllowed {
		t.Fatalf("decision = %s", r.Response.Decision)
	}
	if r.Response.FinalSummary == nil || r.Response.FinalSummary.Digest == "" {
		t.Fatal("response missing final summary")
	}
	// Resource persisted and audit queryable.
	row, err := f.store.GetResource(context.Background(), "Widget", "ns", "w-a1")
	if err != nil {
		t.Fatalf("resource not committed: %v", err)
	}
	var doc map[string]any
	if err := jsonUnmarshal(row.Doc, &doc); err != nil {
		t.Fatal(err)
	}
	sum, err := model.Summarize(doc)
	if err != nil {
		t.Fatal(err)
	}
	if sum.Digest != r.Response.FinalSummary.Digest {
		t.Fatalf("committed doc digest %s != response digest %s (audit/object unbound)",
			sum.Digest, r.Response.FinalSummary.Digest)
	}
	audits, err := f.store.LoadAudits(context.Background(), "a1")
	if err != nil || len(audits) != 1 || audits[0].Resp.FinalSummary.Digest != sum.Digest {
		t.Fatalf("audit binding wrong: %v audits=%v", err, audits)
	}
}

func TestAdmit_DuplicateCallIsIdempotent(t *testing.T) {
	f := newFixture(t, map[string]int64{"Widget": 7})
	ctx := context.Background()
	in := req("dup", map[string]any{"replicas": int64(2)})
	first, err := f.coord.Admit(context.Background(), in)
	if err != nil {
		t.Fatal(err)
	}
	second, err := f.coord.Admit(context.Background(), in)
	if err != nil {
		t.Fatalf("duplicate Admit returned error: %v", err)
	}
	if !second.Stored {
		t.Fatal("second call must replay stored verdict")
	}
	if second.Response.FinalSummary.Digest != first.Response.FinalSummary.Digest {
		t.Fatalf("digests differ: %s vs %s", second.Response.FinalSummary.Digest, first.Response.FinalSummary.Digest)
	}
	// The committed resource counts exactly once in real usage (the hold was
	// consumed at commit, so the ledger holds no residue), and only one audit
	// run exists — proving the duplicate call did not reprocess or double-count.
	used, err := f.q.Used(ctx, "Widget")
	if err != nil {
		t.Fatal(err)
	}
	if used != 2 {
		t.Fatalf("committed usage = %d, want 2 (no double counting)", used)
	}
	var holds int
	if err := f.store.DB().QueryRowContext(ctx,
		`SELECT COUNT(*) FROM quota_ledger WHERE uid = ?`, "dup").Scan(&holds); err != nil {
		t.Fatal(err)
	}
	if holds != 0 {
		t.Fatalf("stale holds = %d, want 0 after commit", holds)
	}
	audits, err := f.store.LoadAudits(ctx, "dup")
	if err != nil || len(audits) != 1 {
		t.Fatalf("want exactly 1 audit run after duplicate call, got %d (err=%v)", len(audits), err)
	}
}

func TestAdmit_QuotaExhaustionDeniesAndReleases(t *testing.T) {
	f := newFixture(t, map[string]int64{"Widget": 3})
	ctx := context.Background()
	// Occupy the full ledger first.
	occupy := req("occ", map[string]any{"replicas": int64(3)})
	if r, err := f.coord.Admit(ctx, occupy); err != nil || r.Response.Decision != model.DecisionAllowed {
		t.Fatalf("occupy: err=%v decision=%s", err, r.Response.Decision)
	}
	// Another 1-replica request overflows.
	r2, err := f.coord.Admit(ctx, req("over", map[string]any{"replicas": int64(1)}))
	if err != nil {
		t.Fatal(err)
	}
	if r2.Response.Decision != model.DecisionDenied || r2.Response.Reason != model.ReasonResourceExhausted {
		t.Fatalf("got %s/%s, want denied/resource_exhausted", r2.Response.Decision, r2.Response.Reason)
	}
	if r2.Response.FinalSummary == nil {
		t.Fatal("denied response must bind the mutated object summary")
	}
	// The denied object must NOT be persisted.
	if _, err := f.store.GetResource(ctx, "Widget", "ns", "w-over"); err == nil {
		t.Fatal("denied resource was committed")
	}
	// Audit persisted with the denial and its steps (replayable).
	audits, _ := f.store.LoadAudits(ctx, "over")
	if len(audits) != 1 || audits[0].Status != storage.StatusDenied {
		t.Fatalf("denial audit missing: %+v", audits)
	}
	if len(audits[0].Resp.Steps) == 0 {
		t.Fatal("audit must retain per-step trail")
	}
}

func TestAdmit_NameCollisionIsStateConflict(t *testing.T) {
	f := newFixture(t, nil)
	ctx := context.Background()
	first := req("one", map[string]any{})
	first.Object["metadata"].(map[string]any)["name"] = "shared"
	if _, err := f.coord.Admit(ctx, first); err != nil {
		t.Fatal(err)
	}
	second := req("two", map[string]any{})
	second.Object["metadata"].(map[string]any)["name"] = "shared"
	_, err := f.coord.Admit(ctx, second)
	if err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("want resource already exists conflict, got %v", err)
	}
}

func TestReconcile_RecoversStaleRow(t *testing.T) {
	f := newFixture(t, map[string]int64{"Widget": 7})
	ctx := context.Background()
	// Insert a request row bypassing Admit's synchronous processing by
	// writing it as pending directly, then let the reconciler claim it.
	in := req("rec1", map[string]any{})
	existed, err := f.store.InsertRequest(ctx, jsonString(in), "rec1", model.OpCreate, 1)
	if err != nil || existed {
		t.Fatalf("insert: existed=%v err=%v", existed, err)
	}
	did, err := f.coord.ReconcileOnce(ctx)
	if err != nil || !did {
		t.Fatalf("reconcile: did=%v err=%v", did, err)
	}
	row, err := f.store.GetRequest(ctx, "rec1")
	if err != nil {
		t.Fatal(err)
	}
	if row.Status != storage.StatusAllowed {
		t.Fatalf("reconciled status = %s, want allowed", row.Status)
	}
	// Nothing left to do.
	did, _ = f.coord.ReconcileOnce(ctx)
	if did {
		t.Fatal("second reconcile must find no work")
	}
}

func TestInvalidInputSynchronous(t *testing.T) {
	f := newFixture(t, nil)
	_, err := f.coord.Admit(context.Background(), model.Request{UID: "", Operation: model.OpCreate})
	if err == nil || !strings.Contains(err.Error(), "uid is required") {
		t.Fatalf("want uid validation error, got %v", err)
	}
}

func TestAdmit_UpdateDeleteQuotaAccountingExact(t *testing.T) {
	f := newFixture(t, map[string]int64{"Widget": 5})
	ctx := context.Background()

	// Create 3.
	c := req("c-3", map[string]any{"replicas": int64(3)})
	c.Object["metadata"].(map[string]any)["name"] = "scale"
	if r, err := f.coord.Admit(ctx, c); err != nil || r.Response.Decision != model.DecisionAllowed {
		t.Fatalf("create: err=%v decision=%s", err, r.Response.Decision)
	}
	if used, _ := f.q.Used(ctx, "Widget"); used != 3 {
		t.Fatalf("after create used=%d want 3", used)
	}

	// Update 3->5 holds only +2 and commits usage=5 (not 3+5, not 3+2+5).
	u := model.Request{UID: "u-5", Operation: model.OpUpdate,
		Object:    namedWidget("scale", 5),
		OldObject: namedWidget("scale", 3)}
	if r, err := f.coord.Admit(ctx, u); err != nil || r.Response.Decision != model.DecisionAllowed {
		t.Fatalf("update: err=%v resp=%+v", err, r.Response)
	}
	if used, _ := f.q.Used(ctx, "Widget"); used != 5 {
		t.Fatalf("after update used=%d want 5", used)
	}

	// Another create of 1 must now be exhausted (5 committed + 1 > 5).
	extra := req("x-1", map[string]any{"replicas": int64(1)})
	extra.Object["metadata"].(map[string]any)["name"] = "extra"
	if r, _ := f.coord.Admit(ctx, extra); r.Response.Reason != model.ReasonResourceExhausted {
		t.Fatalf("want resource_exhausted, got %+v", r.Response)
	}

	// Delete frees everything.
	d := model.Request{UID: "d-1", Operation: model.OpDelete, OldObject: namedWidget("scale", 5)}
	if r, err := f.coord.Admit(ctx, d); err != nil || r.Response.Decision != model.DecisionAllowed {
		t.Fatalf("delete: err=%v resp=%+v", err, r.Response)
	}
	if used, _ := f.q.Used(ctx, "Widget"); used != 0 {
		t.Fatalf("after delete used=%d want 0", used)
	}

	// Recreate at full limit works (deleted capacity returned to the pool).
	rc := req("r-5", map[string]any{"replicas": int64(5)})
	rc.Object["metadata"].(map[string]any)["name"] = "scale"
	if r, err := f.coord.Admit(ctx, rc); err != nil || r.Response.Decision != model.DecisionAllowed {
		t.Fatalf("recreate: err=%v resp=%+v", err, r.Response)
	}
	if used, _ := f.q.Used(ctx, "Widget"); used != 5 {
		t.Fatalf("after recreate used=%d want 5", used)
	}
}

func namedWidget(name string, replicas int64) map[string]any {
	return map[string]any{
		"apiVersion": "v1", "kind": "Widget",
		"metadata": map[string]any{"name": name, "namespace": "ns"},
		"spec":     map[string]any{"replicas": replicas, "capacity": replicas * 100},
	}
}
