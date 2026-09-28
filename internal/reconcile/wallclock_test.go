package reconcile

import (
	"context"
	"testing"
	"time"

	"admission/internal/types"
)

// Uses a real clock (now=nil) and a real background loop, mirroring the HTTP
// server: synchronous attempt 1 + ScheduleRetry, then four timeout attempts
// total must land in the audit trail within the attempt cap.
func TestWallClockTimeoutRetriesFourTimes(t *testing.T) {
	store, pipe, ledger, _ := wallDeps(t)
	defer store.Close()
	svc := wallSvc(t, pipe, store, ledger)
	rec := New(svc, store, Policy{
		MaxAttempts: 4, BaseDelay: 20 * time.Millisecond, Factor: 2,
		MaxDelay: 500 * time.Millisecond,
	}, nil, nil)
	ctx := context.Background()
	go rec.Start(ctx, 5*time.Millisecond)

	// Synchronous attempt 1, then schedule (mirrors HTTP).
	req := wallReview("wall-u")
	resp := svc.Admit(ctx, "wall-1", req, 1)
	if resp.FailureCategory != types.CatTimeout {
		t.Fatalf("attempt1 cat=%s want Timeout", resp.FailureCategory)
	}
	if err := rec.ScheduleRetry(ctx, req, 1); err != nil {
		t.Fatal(err)
	}

	// Wait on durable audit outcomes, not transient queue depth.
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if len(wallAudit(t, store, "wall-u")) >= 4 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	events := wallAudit(t, store, "wall-u")
	if len(events) != 4 {
		for _, e := range events {
			t.Logf("attempt=%d cat=%s term=%v", e.Attempt, e.Category, e.Terminal)
		}
		t.Fatalf("wall-clock attempts=%d want 4", len(events))
	}
	// RecentAudit is newest-first; verify the multiset is attempts 1..4.
	seen := map[int]bool{}
	for _, e := range events {
		if e.Category != types.CatTimeout || e.Attempt < 1 || e.Attempt > 4 {
			t.Fatalf("unexpected event: %+v", e)
		}
		if seen[e.Attempt] {
			t.Fatalf("attempt %d recorded twice", e.Attempt)
		}
		seen[e.Attempt] = true
	}
	if n, _ := store.CountRetry(ctx); n != 0 {
		t.Fatalf("queue should be drained, count=%d", n)
	}
}
