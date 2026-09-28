package storage

import (
	"context"
	"testing"
	"time"

	"admission/internal/types"
)

func openMem(t *testing.T) *SQLiteStore {
	t.Helper()
	ctx := context.Background()
	s, err := Open(ctx, ":memory:")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func sampleResponse(uid string, allowed bool) types.Response {
	return types.Response{
		UID: uid, RunID: "run-1", Allowed: allowed,
		Object:     types.Object{Kind: "Workload", Metadata: types.Metadata{Name: "w"}},
		Final:      types.Summary{Kind: "Workload", Name: "w", Fingerprint: "abc"},
		FinishedAt: time.Unix(1000, 0),
	}
}

func TestResponseUpsertAndLookup(t *testing.T) {
	s := openMem(t)
	ctx := context.Background()

	if _, ok, err := s.LookupUID(ctx, "missing"); err != nil || ok {
		t.Fatalf("LookupUID missing: ok=%v err=%v", ok, err)
	}

	resp := sampleResponse("u1", true)
	if err := s.SaveResponse(ctx, resp); err != nil {
		t.Fatalf("SaveResponse: %v", err)
	}
	got, ok, err := s.LookupUID(ctx, "u1")
	if err != nil || !ok {
		t.Fatalf("LookupUID: ok=%v err=%v", ok, err)
	}
	if got.UID != "u1" || !got.Allowed || got.Final.Fingerprint != "abc" {
		t.Fatalf("stored response mismatch: %+v", got)
	}

	// Upsert with the same UID replaces the verdict exactly once.
	denied := sampleResponse("u1", false)
	denied.DenyReason = "no"
	if err := s.SaveResponse(ctx, denied); err != nil {
		t.Fatalf("SaveResponse upsert: %v", err)
	}
	got2, _, _ := s.LookupUID(ctx, "u1")
	if got2.Allowed || got2.DenyReason != "no" {
		t.Fatalf("upsert did not replace: %+v", got2)
	}
}

func TestAuditAppendAndOrder(t *testing.T) {
	s := openMem(t)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		ev := types.AuditEvent{
			RunID: "run", UID: "u", Attempt: i + 1, Terminal: i == 2,
			Final:      types.Summary{Fingerprint: "f"},
			Steps:      []types.Step{{Plugin: "p", Mutated: true}},
			FinishedAt: time.Unix(int64(i), 0),
		}
		if err := s.AppendAudit(ctx, ev); err != nil {
			t.Fatalf("AppendAudit: %v", err)
		}
	}
	events, err := s.RecentAudit(ctx, 10)
	if err != nil {
		t.Fatalf("RecentAudit: %v", err)
	}
	if len(events) != 3 {
		t.Fatalf("events=%d want 3", len(events))
	}
	if events[0].Attempt != 3 || events[2].Attempt != 1 {
		t.Fatalf("audit not newest-first: %+v", events)
	}
	if len(events[0].Steps) != 1 || events[0].Steps[0].Plugin != "p" {
		t.Fatalf("steps not persisted in audit: %+v", events[0].Steps)
	}
}

func TestRetryQueueLifecycle(t *testing.T) {
	s := openMem(t)
	ctx := context.Background()
	now := time.Unix(5000, 0).UnixNano()

	// Empty queue.
	if _, err := s.DequeueRetry(ctx, now); err != ErrNotFound {
		t.Fatalf("expected ErrNotFound, got %v", err)
	}

	req := types.Review{UID: "u1", Operation: "CREATE"}
	if err := s.EnqueueRetry(ctx, req, 1, now); err != nil {
		t.Fatalf("EnqueueRetry: %v", err)
	}
	// Not due before not_before.
	if _, err := s.DequeueRetry(ctx, now-1); err != ErrNotFound {
		t.Fatalf("expected not due yet, got %v", err)
	}

	item, err := s.DequeueRetry(ctx, now)
	if err != nil {
		t.Fatalf("DequeueRetry: %v", err)
	}
	if item.UID != "u1" || item.Attempts != 1 || item.Review.Operation != "CREATE" {
		t.Fatalf("item mismatch: %+v", item)
	}

	// Dequeue claims the item: a second dequeue must not return it.
	if _, err := s.DequeueRetry(ctx, now+1); err != ErrNotFound {
		t.Fatalf("claimed item dequeued twice: %v", err)
	}

	// Requeue for attempt 2 later.
	future := time.Unix(6000, 0).UnixNano()
	if err := s.RequeueRetry(ctx, "u1", 2, future); err != nil {
		t.Fatalf("RequeueRetry: %v", err)
	}
	if n, _ := s.CountRetry(ctx); n != 1 {
		t.Fatalf("CountRetry=%d want 1", n)
	}
	if _, err := s.DequeueRetry(ctx, future-1); err != ErrNotFound {
		t.Fatalf("requeued item due too early: %v", err)
	}
	item2, err := s.DequeueRetry(ctx, future)
	if err != nil || item2.Attempts != 2 {
		t.Fatalf("second dequeue: %+v err=%v", item2, err)
	}

	// Done removes the row.
	if err := s.DoneRetry(ctx, "u1"); err != nil {
		t.Fatalf("DoneRetry: %v", err)
	}
	if n, _ := s.CountRetry(ctx); n != 0 {
		t.Fatalf("queue should be empty, count=%d", n)
	}
}

func TestEnqueueRetryIsUpsert(t *testing.T) {
	s := openMem(t)
	ctx := context.Background()
	req := types.Review{UID: "u1", Operation: "CREATE"}
	if err := s.EnqueueRetry(ctx, req, 1, 1); err != nil {
		t.Fatal(err)
	}
	if err := s.EnqueueRetry(ctx, req, 2, 2); err != nil {
		t.Fatal(err)
	}
	if n, _ := s.CountRetry(ctx); n != 1 {
		t.Fatalf("duplicate enqueue created two rows: %d", n)
	}
}
