package replay

import (
	"context"
	"encoding/json"
	"path/filepath"
	"testing"

	"flexhash/internal/fherr"
	"flexhash/internal/hashring"
	"flexhash/internal/store"
)

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

func bootStore(t *testing.T) (*store.Store, context.Context) {
	t.Helper()
	dir := t.TempDir()
	ctx := context.Background()
	st, err := store.Open(ctx, filepath.Join(dir, "r.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st, ctx
}

func persist(t *testing.T, ctx context.Context, st *store.Store, mgr *hashring.Manager, ring *hashring.Ring, version int64, members []hashring.Member) {
	t.Helper()
	rows := make([]store.AssignmentRow, 0, ring.BucketCount)
	for _, a := range ring.Assignments() {
		rows = append(rows, store.AssignmentRow{Version: version, Bucket: a.Bucket, Member: a.Member})
	}
	mj := mustJSON(members)
	if err := st.SaveConfig(ctx, store.ConfigSnapshot{
		Version: version, BucketCount: ring.BucketCount, MembersJSON: mj,
	}, rows); err != nil {
		t.Fatalf("save v%d: %v", version, err)
	}
}

func TestReplayRebuildsVersionsAndHealth(t *testing.T) {
	st, ctx := bootStore(t)
	B := 256
	mgr := hashring.NewManager(B)
	ms1 := []hashring.Member{
		{ID: "a", Address: "h", Weight: 1, Healthy: true},
		{ID: "b", Address: "h", Weight: 1, Healthy: true},
		{ID: "c", Address: "h", Weight: 1, Healthy: true},
	}
	r1, _, _ := mgr.Bootstrap(1, ms1, 0)
	persist(t, ctx, st, mgr, r1, 1, ms1)

	if err := st.SaveHealth(ctx, 1, "a", false); err != nil {
		t.Fatal(err)
	}
	ms2 := []hashring.Member{
		{ID: "a", Address: "h", Weight: 1, Healthy: false},
		{ID: "b", Address: "h", Weight: 1, Healthy: true},
		{ID: "c", Address: "h", Weight: 1, Healthy: true},
		{ID: "d", Address: "h", Weight: 1, Healthy: true},
	}
	r2, _, err := mgr.ApplyConfig(2, ms2)
	if err != nil {
		t.Fatal(err)
	}
	persist(t, ctx, st, mgr, r2, 2, ms2)
	if err := st.SaveHealth(ctx, 2, "a", true); err != nil {
		t.Fatal(err)
	}

	mgr2, rep, err := Rebuild(ctx, st)
	if err != nil {
		t.Fatalf("rebuild: %v", err)
	}
	if rep.EventsReplayed != 4 {
		t.Fatalf("events=%d want 4 (2 config + 2 health)", rep.EventsReplayed)
	}
	ring, hrev := mgr2.Current()
	if ring.Version != 2 || hrev != 2 {
		t.Fatalf("replayed state v%d rev%d want v2 rev2", ring.Version, hrev)
	}
	if !ring.Members["a"].Healthy {
		t.Fatalf("health of a after replay should be healthy (rev2)")
	}
	if q := ring.Quota(); q["a"]+q["b"]+q["c"]+q["d"] != B {
		t.Fatalf("quota not saturated: %v", q)
	}
}

func TestVerifyCleanLogHasNoMismatch(t *testing.T) {
	st, ctx := bootStore(t)
	B := 512
	mgr := hashring.NewManager(B)
	var prev *hashring.Ring
	for v, ms := range [][]hashring.Member{
		{{ID: "a", Address: "h", Weight: 3, Healthy: true}, {ID: "b", Address: "h", Weight: 2, Healthy: true}},
		{{ID: "a", Address: "h", Weight: 3, Healthy: true}, {ID: "b", Address: "h", Weight: 2, Healthy: true}, {ID: "c", Address: "h", Weight: 1, Healthy: true}},
		{{ID: "a", Address: "h", Weight: 1, Healthy: true}, {ID: "c", Address: "h", Weight: 1, Healthy: true}},
	} {
		var r *hashring.Ring
		if v == 0 {
			r, _, _ = mgr.Bootstrap(1, ms, 0)
		} else {
			r, _, _ = mgr.ApplyConfig(int64(v+1), ms)
		}
		persist(t, ctx, st, mgr, r, int64(v+1), ms)
		prev = r
	}
	_ = prev
	_, rep, err := VerifyAssignments(ctx, st)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if len(rep.Mismatches) != 0 {
		t.Fatalf("clean log produced mismatches: %v", rep.Mismatches)
	}
}

func TestVerifyDetectsCorruptedAssignment(t *testing.T) {
	st, ctx := bootStore(t)
	B := 128
	mgr := hashring.NewManager(B)
	ms := []hashring.Member{
		{ID: "a", Address: "h", Weight: 1, Healthy: true},
		{ID: "b", Address: "h", Weight: 1, Healthy: true},
	}
	r1, _, _ := mgr.Bootstrap(1, ms, 0)
	persist(t, ctx, st, mgr, r1, 1, ms)

	// Corrupt storage directly: flip the owner of bucket 0 to the other
	// member. Read its real owner first so the UPDATE is guaranteed to hit.
	var owner string
	if err := st.DB().QueryRowContext(ctx,
		`SELECT member FROM assignments WHERE version=1 AND bucket=0`).Scan(&owner); err != nil {
		t.Fatal(err)
	}
	bad := "b"
	if owner == "b" {
		bad = "a"
	}
	if _, err := st.DB().ExecContext(ctx,
		`UPDATE assignments SET member=? WHERE version=1 AND bucket=0`, bad); err != nil {
		t.Fatal(err)
	}
	_, rep, err := VerifyAssignments(ctx, st)
	if err != nil {
		t.Fatalf("verify should report mismatch, not error: %v", err)
	}
	if len(rep.Mismatches) == 0 {
		t.Fatal("corruption not detected: expected at least one mismatch")
	}
}

func TestReplayEmptyLogIsConflict(t *testing.T) {
	st, ctx := bootStore(t)
	_, _, err := Rebuild(ctx, st)
	if fherr.KindOf(err) != fherr.KindStateConflict {
		t.Fatalf("empty log kind=%v, want state_conflict", fherr.KindOf(err))
	}
}

func TestReplayDeterministicAcrossRebuilds(t *testing.T) {
	st, ctx := bootStore(t)
	B := 300
	mgr := hashring.NewManager(B)
	ms := []hashring.Member{
		{ID: "a", Address: "h", Weight: 2, Healthy: true},
		{ID: "b", Address: "h", Weight: 1, Healthy: true},
		{ID: "c", Address: "h", Weight: 1, Healthy: true},
	}
	r1, _, _ := mgr.Bootstrap(1, ms, 0)
	persist(t, ctx, st, mgr, r1, 1, ms)

	first := owners(mustRebuild(t, ctx, st))
	for i := 0; i < 5; i++ {
		if got := owners(mustRebuild(t, ctx, st)); !equal(got, first) {
			t.Fatalf("rebuild %d nondeterministic", i)
		}
	}
}

func mustRebuild(t *testing.T, ctx context.Context, st *store.Store) *hashring.Ring {
	t.Helper()
	mgr, _, err := Rebuild(ctx, st)
	if err != nil {
		t.Fatal(err)
	}
	r, _ := mgr.Current()
	return r
}

func owners(r *hashring.Ring) []string {
	out := make([]string, r.BucketCount)
	for i := 0; i < r.BucketCount; i++ {
		out[i] = r.Owner(i)
	}
	return out
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
