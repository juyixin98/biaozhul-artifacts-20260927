package reconcile

import (
	"testing"
	"time"
)

func TestQueueDedupesRepeatedAdds(t *testing.T) {
	q := newQueue()
	defer q.Shutdown()
	for i := 0; i < 10; i++ {
		q.Add("a") // repeated watch events for the same resource
	}
	if q.Len() != 1 {
		t.Fatalf("queue len = %d, want 1", q.Len())
	}
	key, ok := q.Get()
	if !ok || key != "a" {
		t.Fatalf("get = %q,%v", key, ok)
	}
	// While in flight, adds collapse and do not append a second copy.
	q.Add("a")
	q.Add("a")
	if q.Len() != 0 {
		t.Fatalf("queue len while in flight = %d, want 0", q.Len())
	}
	q.Done("a")
	if q.Len() != 1 {
		t.Fatalf("queue len after done = %d, want 1 (dirty item requeued)", q.Len())
	}
}

func TestQueueAddAfter(t *testing.T) {
	q := newQueue()
	defer q.Shutdown()
	start := time.Now()
	q.AddAfter("b", 30*time.Millisecond)
	if q.Len() != 0 {
		t.Fatal("delayed item must not be immediately ready")
	}
	key, ok := q.Get()
	if !ok || key != "b" {
		t.Fatalf("get = %q,%v", key, ok)
	}
	if elapsed := time.Since(start); elapsed < 20*time.Millisecond {
		t.Fatalf("item ready after %v, want >= 30ms delay", elapsed)
	}
	q.Done("b")
}

func TestQueueShutdownUnblocks(t *testing.T) {
	q := newQueue()
	done := make(chan struct{})
	go func() {
		_, _ = q.Get()
		close(done)
	}()
	q.Shutdown()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("Get did not unblock on shutdown")
	}
}
