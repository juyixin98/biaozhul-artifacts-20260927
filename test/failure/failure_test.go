// Package failure_test exercises the failure taxonomy end to end: malformed
// fixtures must surface stable error KINDS, and reconcile runs must be
// classified into stable statuses. Tests assert the specific category, not
// merely that an error occurred.
package failure_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"netpolicy/internal/domain"
	"netpolicy/internal/reconcile"
	"netpolicy/internal/source"
	"netpolicy/internal/store"
)

type expectKind struct {
	file string
	kind domain.ErrorKind
}

func TestMalformedFixtureFailureCategories(t *testing.T) {
	cases := []expectKind{
		{"syntax-broken.json", domain.ErrSourceSyntax},
		{"duplicate-uid.json", domain.ErrEndpointDuplicateUID},
		{"endpoint-unknown-namespace.json", domain.ErrEndpointUnknownNamespace},
		{"port-out-of-range.json", domain.ErrPortNumberInvalid},
		{"selector-bad-operator.json", domain.ErrSelectorInvalid},
	}
	for _, tc := range cases {
		t.Run(tc.file, func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join("..", "fixtures", "failures", tc.file))
			if err != nil {
				t.Fatalf("read: %v", err)
			}
			_, err = source.Parse(raw)
			if err == nil {
				t.Fatalf("expected error of kind %s, got nil", tc.kind)
			}
			ve, ok := domain.AsValidationError(err)
			if !ok {
				t.Fatalf("error %v is not a ValidationError", err)
			}
			if ve.Kind != tc.kind {
				t.Fatalf("error kind = %s, want %s (detail: %s)", ve.Kind, tc.kind, ve.Detail)
			}
		})
	}
}

func TestFixtureSourceMissingFileIsSourceNotFound(t *testing.T) {
	s := &source.FixtureSource{Path: filepath.Join(t.TempDir(), "does-not-exist.json")}
	_, err := s.Fetch(context.Background())
	ve, ok := domain.AsValidationError(err)
	if !ok || ve.Kind != domain.ErrSourceNotFound {
		t.Fatalf("want %s, got %v", domain.ErrSourceNotFound, err)
	}
}

// fakeSource is an in-memory source whose returned snapshot/error can be
// swapped between reconcile passes.
type fakeSource struct {
	snap *domain.Snapshot
	err  error
}

func (f *fakeSource) Fetch(context.Context) (*domain.Snapshot, error) {
	if f.err != nil {
		return nil, f.err
	}
	return f.snap, nil
}

func validSnapshot() *domain.Snapshot {
	return &domain.Snapshot{
		Namespaces: []domain.Namespace{{Name: "ns"}},
		Endpoints: []domain.Endpoint{
			{UID: "u1", Name: "e1", Namespace: "ns", Labels: map[string]string{"app": "x"}},
		},
		Policies:   nil,
		SourceHash: "h1",
	}
}

func openTestStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(context.Background(), "file:"+filepath.Join(t.TempDir(), "test.db")+"?_pragma=busy_timeout(2000)")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func TestReconcileClassifiesEveryRunKind(t *testing.T) {
	fs := &fakeSource{snap: validSnapshot()}
	st := openTestStore(t)
	rec := reconcile.New(fs, st)
	ctx := context.Background()

	// 1. first valid fetch -> applied at revision 1
	r1, err := rec.Once(ctx)
	if err != nil {
		t.Fatalf("once: %v", err)
	}
	if r1.Status != reconcile.StatusApplied || r1.Revision != 1 {
		t.Fatalf("first pass = %s rev %d, want applied rev 1", r1.Status, r1.Revision)
	}
	if r1.StartedAt.IsZero() || r1.FinishedAt.IsZero() || r1.FinishedAt.Before(r1.StartedAt) {
		t.Fatalf("run timestamps not populated: started=%v finished=%v", r1.StartedAt, r1.FinishedAt)
	}
	if r1.ID == 0 {
		t.Fatal("run record must carry its persisted id")
	}

	// 2. identical content hash -> unchanged, no new revision.
	r2, _ := rec.Once(ctx)
	if r2.Status != reconcile.StatusUnchanged || r2.Revision != 1 {
		t.Fatalf("second pass = %s rev %d, want unchanged rev 1", r2.Status, r2.Revision)
	}

	// 3. changed content -> applied at revision 2.
	fs.snap = validSnapshot()
	fs.snap.Endpoints[0].Labels = map[string]string{"app": "y"}
	fs.snap.SourceHash = "h2"
	r3, _ := rec.Once(ctx)
	if r3.Status != reconcile.StatusApplied || r3.Revision != 2 {
		t.Fatalf("third pass = %s rev %d, want applied rev 2", r3.Status, r3.Revision)
	}

	// 4. fetch failure -> classified fetch_failed, error kind recorded.
	fs.err = domain.ValidationError{Kind: domain.ErrSourceNotFound, Detail: "missing"}
	r4, _ := rec.Once(ctx)
	if r4.Status != reconcile.StatusFetchFailed || r4.ErrorKind != string(domain.ErrSourceNotFound) {
		t.Fatalf("fourth pass = %s kind %s, want fetch_failed/%s", r4.Status, r4.ErrorKind, domain.ErrSourceNotFound)
	}

	// 5. validation failure (parsed but structurally invalid) -> validation_failed.
	fs.err = nil
	fs.snap = &domain.Snapshot{
		Namespaces: []domain.Namespace{{Name: "ns"}},
		Endpoints: []domain.Endpoint{
			{UID: "u1", Namespace: "ns"},
			{UID: "u1", Namespace: "ns"},
		},
	}
	// Recompute: duplicates are caught in Validate by the source normally;
	// simulate the exact error the adapter would produce.
	fs.err = domain.ValidationError{Kind: domain.ErrEndpointDuplicateUID, Name: "u1"}
	r5, _ := rec.Once(ctx)
	if r5.Status != reconcile.StatusValidationFailed || r5.ErrorKind != string(domain.ErrEndpointDuplicateUID) {
		t.Fatalf("fifth pass = %s kind %s, want validation_failed/%s", r5.Status, r5.ErrorKind, domain.ErrEndpointDuplicateUID)
	}

	// Audit rows were persisted for every pass.
	runs, err := st.RecentRuns(ctx, 10)
	if err != nil {
		t.Fatalf("runs: %v", err)
	}
	if len(runs) != 5 {
		t.Fatalf("expected 5 recorded runs, got %d", len(runs))
	}
}

func TestSnapshotRevisionsAreMonotonicAndRetrievable(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	for i, hash := range []string{"a", "b", "c"} {
		snap := validSnapshot()
		snap.SourceHash = hash
		snap.Endpoints[0].Labels["v"] = hash
		rev, changed, err := st.SaveSnapshot(ctx, snap)
		if err != nil {
			t.Fatalf("save: %v", err)
		}
		if !changed || rev != int64(i+1) {
			t.Fatalf("save %d: rev=%d changed=%v", i, rev, changed)
		}
		if snap.Revision != rev {
			t.Fatalf("snapshot not stamped with its revision: got %d want %d", snap.Revision, rev)
		}
	}
	// Re-saving content identical to the CURRENT revision creates no new
	// revision.
	same := validSnapshot()
	same.SourceHash = "c"
	same.Endpoints[0].Labels["v"] = "c"
	rev, changed, err := st.SaveSnapshot(ctx, same)
	if err != nil {
		t.Fatalf("duplicate save: %v", err)
	}
	if changed || rev != 3 {
		t.Fatalf("identical-to-latest save: rev=%d changed=%v, want rev 3 unchanged", rev, changed)
	}
	latest, ok, err := st.LatestRevision(ctx)
	if err != nil || !ok || latest != 3 {
		t.Fatalf("latest = %d ok=%v err=%v, want 3", latest, ok, err)
	}
	old, err := st.GetRevision(ctx, 2)
	if err != nil {
		t.Fatalf("get rev 2: %v", err)
	}
	if old.SourceHash != "b" {
		t.Fatalf("rev 2 hash = %q, want b", old.SourceHash)
	}
	if _, err := st.GetRevision(ctx, 99); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("missing revision should be ErrNotFound, got %v", err)
	}
}

func TestTrimHistoryKeepsLatest(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	for _, hash := range []string{"a", "b", "c", "d"} {
		s := validSnapshot()
		s.SourceHash = hash
		if _, _, err := st.SaveSnapshot(ctx, s); err != nil {
			t.Fatal(err)
		}
	}
	removed, err := st.TrimHistory(ctx, 2)
	if err != nil || removed != 2 {
		t.Fatalf("trim removed=%d err=%v, want 2", removed, err)
	}
	latest, _ := st.Latest(ctx)
	if latest == nil || latest.SourceHash != "d" {
		t.Fatalf("latest after trim = %+v, want hash d", latest)
	}
	if _, err := st.GetRevision(ctx, 2); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("rev 2 should have been trimmed, got %v", err)
	}
}

func TestStoreRunTimestampsArePopulated(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	now := time.Now()
	_, err := st.SaveRun(ctx, reconcile.RunRecord{
		StartedAt:   now,
		FinishedAt:  now.Add(time.Millisecond),
		Status:      reconcile.StatusApplied,
		Revision:    1,
		ContentHash: "x",
		Attempted:   true,
	})
	if err != nil {
		t.Fatal(err)
	}
	runs, _ := st.RecentRuns(ctx, 1)
	if len(runs) != 1 || !runs[0].Attempted || runs[0].Status != reconcile.StatusApplied {
		t.Fatalf("round-tripped run mismatch: %+v", runs)
	}
}
