package reconcile

import (
	"testing"
	"time"
)

func TestQueueDeduplicatesConcurrentEvents(t *testing.T) {
	// Duplicate Add calls while the key is queued collapse to one delivery.
	q := NewQueue(Backoff{Base: time.Millisecond, Max: 10 * time.Millisecond})
	defer q.ShutDown()

	q.Add("k1")
	q.Add("k1")
	q.Add("k1")

	k, ok := q.Get()
	if !ok || k != "k1" {
		t.Fatalf("expected k1, got %q ok=%v", k, ok)
	}
	// Add while processing marks dirty but must not deliver twice before Done.
	q.Add("k1")
	q.Add("k1")
	q.Done("k1")

	k, ok = q.Get()
	if !ok || k != "k1" {
		t.Fatalf("expected one re-delivery of k1, got %q ok=%v", k, ok)
	}
	q.Done("k1")

	// No third delivery.
	time.Sleep(30 * time.Millisecond)
	if st := q.Stats(); st.Ready != 0 {
		t.Fatalf("expected empty ready queue, got %+v", st)
	}
}

func TestQueueBackoffGrows(t *testing.T) {
	b := Backoff{Base: 10 * time.Millisecond, Max: time.Second}
	cases := []struct {
		retries int
		want    time.Duration
	}{
		{0, 10 * time.Millisecond},
		{1, 20 * time.Millisecond},
		{3, 80 * time.Millisecond},
		{20, time.Second},
	}
	for _, tc := range cases {
		if got := b.delay(tc.retries); got != tc.want {
			t.Fatalf("retries=%d: got %v want %v", tc.retries, got, tc.want)
		}
	}
}
