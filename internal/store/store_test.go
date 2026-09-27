package store_test

import (
	"context"
	"testing"

	"flowrouter/internal/apperr"
	"flowrouter/internal/store"
)

func openStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(context.Background(), ":memory:", 50)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

func TestRingVersionRoundTrip(t *testing.T) {
	ctx := context.Background()
	st := openStore(t)
	err := st.InsertRingVersion(ctx, store.RingVersionRow{
		Version: 1, MembersJSON: []byte(`[{"id":"a"}]`),
		AllocationJSON: []byte(`{"total":1,"counts":{"a":1}}`),
		Fingerprint:    "fp1",
	})
	if err != nil {
		t.Fatal(err)
	}
	got, err := st.RingVersion(ctx, 1)
	if err != nil {
		t.Fatal(err)
	}
	if got.Fingerprint != "fp1" || got.Version != 1 {
		t.Fatalf("row=%+v", got)
	}
	if string(got.MembersJSON) != `[{"id":"a"}]` {
		t.Fatalf("members=%s", got.MembersJSON)
	}
}

func TestUnknownVersionIsConflict(t *testing.T) {
	st := openStore(t)
	_, err := st.RingVersion(context.Background(), 42)
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindStateConflict || ae.Code != "UNKNOWN_VERSION" {
		t.Fatalf("err=%v, want STATE_CONFLICT/UNKNOWN_VERSION", err)
	}
}

func TestUnknownRunIsConflict(t *testing.T) {
	st := openStore(t)
	_, err := st.GetRun(context.Background(), "run-nope")
	if ae, ok := apperr.As(err); !ok || ae.Code != "UNKNOWN_RUN" {
		t.Fatalf("err=%v", err)
	}
}

func TestBadJSONRejectedAsInvalidInput(t *testing.T) {
	st := openStore(t)
	err := st.InsertRingVersion(context.Background(), store.RingVersionRow{
		Version: 1, MembersJSON: []byte(`{not json`), AllocationJSON: []byte(`{}`),
	})
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindInvalidInput || ae.Code != "BAD_JSON" {
		t.Fatalf("err=%v, want INVALID_INPUT/BAD_JSON", err)
	}
}

func TestRunLifecycleAndDecisions(t *testing.T) {
	ctx := context.Background()
	st := openStore(t)
	if err := st.InsertRun(ctx, store.RunRow{
		RunID: "run-1", FlowSetName: "fs", FlowCount: 2,
		FromVersion: 1, ToVersion: 2, Status: "RUNNING",
	}); err != nil {
		t.Fatal(err)
	}
	decs := []store.DecisionRow{
		{RunID: "run-1", Version: 1, FlowKey: "k1", FlowHash: 11, MemberID: "a", Reason: "replay"},
		{RunID: "run-1", Version: 2, FlowKey: "k2", FlowHash: 22, MemberID: "b", Reason: "replay"},
	}
	if err := st.InsertDecisions(ctx, decs); err != nil {
		t.Fatal(err)
	}
	if err := st.InsertMigrations(ctx, []store.MigrationRow{
		{RunID: "run-1", FlowKey: "k1", FlowHash: 11, OldMember: "c", NewMember: "a", Reason: "member_added"},
	}); err != nil {
		t.Fatal(err)
	}
	if err := st.UpdateRunResult(ctx, "run-1", "COMPLETED",
		[]byte(`{"moved":1}`), "", "", ""); err != nil {
		t.Fatal(err)
	}
	r, err := st.GetRun(ctx, "run-1")
	if err != nil {
		t.Fatal(err)
	}
	if r.Status != "COMPLETED" || string(r.Summary) != `{"moved":1}` {
		t.Fatalf("run=%+v summary=%s", r, r.Summary)
	}
	migs, err := st.RunMigrations(ctx, "run-1")
	if err != nil || len(migs) != 1 || migs[0].OldMember != "c" {
		t.Fatalf("migrations=%v err=%v", migs, err)
	}
	runs, err := st.ListRuns(ctx, 10)
	if err != nil || len(runs) != 1 {
		t.Fatalf("list=%v err=%v", runs, err)
	}
}

// TestBusyDatabaseMapsToResourceExhausted proves the mandated failure-class
// mapping: a write that cannot acquire a lock within busy_timeout must surface
// as RESOURCE_EXHAUSTED/STORE_BUSY, distinct from input/compute failures.
func TestBusyDatabaseMapsToResourceExhausted(t *testing.T) {
	// File-backed DB with a SECOND connection holding an exclusive write lock;
	// the service store (busy_timeout tiny) must then fail with STORE_BUSY.
	dir := t.TempDir()
	path := dir + "/busy.db"

	holder, err := store.Open(context.Background(), path, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = holder.Close() })

	// Hold a write transaction open on the raw single connection. Begin
	// directly and leave it uncommitted.
	ctx := context.Background()
	conn, err := holder.DB().Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(ctx, "BEGIN EXCLUSIVE"); err != nil {
		t.Fatalf("begin exclusive: %v", err)
	}
	t.Cleanup(func() {
		_, _ = conn.ExecContext(ctx, "ROLLBACK")
		_ = conn.Close()
	})

	victim, err := store.Open(ctx, path, 10) // 10ms busy timeout
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = victim.Close() })

	err = victim.InsertRingVersion(ctx, store.RingVersionRow{
		Version: 1, MembersJSON: []byte(`[]`), AllocationJSON: []byte(`{}`),
	})
	ae, ok := apperr.As(err)
	if !ok {
		t.Fatalf("err=%v, want structured error", err)
	}
	if ae.Kind != apperr.KindResourceExhausted {
		t.Fatalf("kind=%s, want RESOURCE_EXHAUSTED", ae.Kind)
	}
	if ae.Code != "STORE_BUSY" {
		t.Fatalf("code=%s, want STORE_BUSY (got msg=%s)", ae.Code, ae.Message)
	}
}

func TestMaxRingVersionEmptyIsZero(t *testing.T) {
	st := openStore(t)
	v, err := st.MaxRingVersion(context.Background())
	if err != nil || v != 0 {
		t.Fatalf("v=%d err=%v", v, err)
	}
}
