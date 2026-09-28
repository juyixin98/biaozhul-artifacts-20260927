package reconcile

import (
	"context"
	"sync"
	"testing"
	"time"

	"netpolicy/internal/domain"
)

type memStore struct {
	mu       sync.Mutex
	snaps    map[int64]*domain.Snapshot
	runs     []RunRecord
	failSave bool
}

func newMemStore() *memStore { return &memStore{snaps: map[int64]*domain.Snapshot{}} }

func (m *memStore) SaveSnapshot(_ context.Context, snap *domain.Snapshot) (int64, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.failSave {
		return 0, false, context.DeadlineExceeded
	}
	for rev, ex := range m.snaps {
		if ex.SourceHash == snap.SourceHash {
			snap.Revision = rev
			return rev, false, nil
		}
	}
	rev := int64(len(m.snaps) + 1)
	snap.Revision = rev
	cp := *snap
	m.snaps[rev] = &cp
	return rev, true, nil
}

func (m *memStore) SaveRun(_ context.Context, r RunRecord) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.runs = append(m.runs, r)
	return int64(len(m.runs)), nil
}

func (m *memStore) runCount() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.runs)
}

type swapSource struct {
	mu   sync.Mutex
	snap *domain.Snapshot
	err  error
}

func (s *swapSource) Fetch(context.Context) (*domain.Snapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return nil, s.err
	}
	cp := *s.snap
	return &cp, nil
}

func TestLoopRunsImmediatelyAndOnTrigger(t *testing.T) {
	src := &swapSource{snap: &domain.Snapshot{SourceHash: "h1"}}
	st := newMemStore()
	rec := New(src, st)
	loop := NewLoop(rec, 0, nil)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { loop.Run(ctx); close(done) }()

	waitUntil(t, time.Second, func() bool { return st.runCount() == 1 })
	if r, ok := loop.Last(); !ok || r.Status != StatusApplied {
		t.Fatalf("initial run = %+v ok=%v", r, ok)
	}

	// Identical content -> unchanged, still a recorded run.
	loop.Trigger()
	waitUntil(t, time.Second, func() bool { return st.runCount() == 2 })
	if r, _ := loop.Last(); r.Status != StatusUnchanged {
		t.Fatalf("want unchanged, got %s", r.Status)
	}

	// Changed content -> applied again.
	src.mu.Lock()
	src.snap.SourceHash = "h2"
	src.mu.Unlock()
	loop.Trigger()
	waitUntil(t, time.Second, func() bool {
		r, ok := loop.Last()
		return ok && r.Status == StatusApplied && r.Revision == 2
	})

	// Fetch error -> last record is a failure but the loop keeps running.
	src.mu.Lock()
	src.err = domain.ValidationError{Kind: domain.ErrSourceSyntax}
	src.mu.Unlock()
	loop.Trigger()
	waitUntil(t, time.Second, func() bool {
		r, ok := loop.Last()
		return ok && r.Status == StatusFetchFailed && r.ErrorKind == string(domain.ErrSourceSyntax)
	})

	cancel()
	<-done
}

func TestTriggersCoalesce(t *testing.T) {
	st := newMemStore()
	src := &swapSource{snap: &domain.Snapshot{SourceHash: "h"}}
	rec := New(src, st)
	loop := NewLoop(rec, time.Hour, nil)
	for i := 0; i < 10; i++ {
		loop.Trigger()
	}
	// A second trigger while one is queued must not block or panic.
	loop.Trigger()
}

func waitUntil(t *testing.T, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatal("condition not met before timeout")
}
