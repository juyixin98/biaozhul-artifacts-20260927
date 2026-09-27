package store

import (
	"context"
	"path/filepath"
	"testing"

	"flexhash/internal/fherr"
)

func openTempStore(t *testing.T) (*Store, context.Context) {
	t.Helper()
	dir := t.TempDir()
	ctx := context.Background()
	st, err := Open(ctx, filepath.Join(dir, "test.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st, ctx
}

func TestSaveAndLoadRoundTrip(t *testing.T) {
	st, ctx := openTempStore(t)
	snap := ConfigSnapshot{
		Version: 1, BucketCount: 4,
		MembersJSON: []byte(`[{"id":"a","address":"h","weight":1,"healthy":true}]`),
	}
	rows := []AssignmentRow{
		{Version: 1, Bucket: 0, Member: "a"},
		{Version: 1, Bucket: 1, Member: "a"},
		{Version: 1, Bucket: 2, Member: "a"},
		{Version: 1, Bucket: 3, Member: "a"},
	}
	if err := st.SaveConfig(ctx, snap, rows); err != nil {
		t.Fatalf("save: %v", err)
	}
	got, gotRows, err := st.LatestConfig(ctx)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.Version != 1 || got.BucketCount != 4 || len(gotRows) != 4 {
		t.Fatalf("round trip mismatch: %+v rows=%d", got, len(gotRows))
	}

	evs, err := st.Events(ctx)
	if err != nil {
		t.Fatalf("events: %v", err)
	}
	if len(evs) != 1 || evs[0].Type != EvConfig || evs[0].Seq != 1 {
		t.Fatalf("event log wrong: %+v", evs)
	}
}

func TestSaveConfigRowCountMismatchIsComputationFailure(t *testing.T) {
	st, ctx := openTempStore(t)
	snap := ConfigSnapshot{Version: 1, BucketCount: 4, MembersJSON: []byte("[]")}
	err := st.SaveConfig(ctx, snap, []AssignmentRow{{Version: 1, Bucket: 0, Member: "a"}})
	if k := fherr.KindOf(err); k != fherr.KindComputationFailed {
		t.Fatalf("kind=%v err=%v, want computation_failed", k, err)
	}
}

func TestDuplicateVersionIsStateConflict(t *testing.T) {
	st, ctx := openTempStore(t)
	snap := ConfigSnapshot{Version: 1, BucketCount: 1, MembersJSON: []byte("[]")}
	rows := []AssignmentRow{{Version: 1, Bucket: 0, Member: "a"}}
	if err := st.SaveConfig(ctx, snap, rows); err != nil {
		t.Fatal(err)
	}
	if err := st.SaveConfig(ctx, snap, rows); fherr.KindOf(err) != fherr.KindStateConflict {
		t.Fatalf("duplicate version kind=%v err=%v, want state_conflict", fherr.KindOf(err), err)
	}
}

func TestHealthEventsOrdered(t *testing.T) {
	st, ctx := openTempStore(t)
	bootstrapOneMember(t, st, ctx)
	if err := st.SaveHealth(ctx, 1, "a", false); err != nil {
		t.Fatal(err)
	}
	if err := st.SaveHealth(ctx, 2, "a", true); err != nil {
		t.Fatal(err)
	}
	rev, err := st.LatestHealthRevision(ctx)
	if err != nil || rev != 2 {
		t.Fatalf("rev=%d err=%v", rev, err)
	}
	evs, _ := st.Events(ctx)
	if len(evs) != 3 || evs[1].Type != EvHealth || evs[2].Type != EvHealth {
		t.Fatalf("event order wrong: %+v", evs)
	}
}

func TestRunLogsRoundTrip(t *testing.T) {
	st, ctx := openTempStore(t)
	if err := st.SaveRunLog(ctx, "run-1", "TestX", "passed",
		map[string]any{"moved": 3}); err != nil {
		t.Fatal(err)
	}
	logs, err := st.RunLogs(ctx, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(logs) != 1 || logs[0].RunID != "run-1" || logs[0].Result != "passed" {
		t.Fatalf("run logs wrong: %+v", logs)
	}
}

func TestLatestConfigEmptyIsConflict(t *testing.T) {
	st, ctx := openTempStore(t)
	if _, _, err := st.LatestConfig(ctx); fherr.KindOf(err) != fherr.KindStateConflict {
		t.Fatalf("empty latest kind=%v, want state_conflict", fherr.KindOf(err))
	}
}

func bootstrapOneMember(t *testing.T, st *Store, ctx context.Context) {
	t.Helper()
	snap := ConfigSnapshot{Version: 1, BucketCount: 1,
		MembersJSON: []byte(`[{"id":"a","address":"h","weight":1,"healthy":true}]`)}
	if err := st.SaveConfig(ctx, snap, []AssignmentRow{{Version: 1, Bucket: 0, Member: "a"}}); err != nil {
		t.Fatal(err)
	}
}
