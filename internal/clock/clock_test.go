package clock_test

import (
	"sync"
	"testing"

	"clsnap/internal/clock"
)

func TestLamportTickAndObserve(t *testing.T) {
	c := clock.New()
	if got := c.Peek(); got != 0 {
		t.Fatalf("initial clock = %d, want 0", got)
	}
	if got := c.Tick(); got != 1 {
		t.Fatalf("first tick = %d, want 1", got)
	}
	// Observe a LOWER timestamp: local just advances by one.
	if got := c.Observe(1); got != 2 {
		t.Fatalf("observe lower = %d, want 2", got)
	}
	// Observe a HIGHER timestamp: max then +1.
	if got := c.Observe(10); got != 11 {
		t.Fatalf("observe higher = %d, want 11", got)
	}
	// Tie then +1.
	if got := c.Observe(11); got != 12 {
		t.Fatalf("observe equal = %d, want 12", got)
	}
}

func TestLamportConcurrent(t *testing.T) {
	c := clock.New()
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				v := c.Tick()
				if v == 0 {
					t.Error("tick returned zero under concurrency")
				}
			}
		}()
	}
	wg.Wait()
	// After concurrent writers settle, one more tick must advance exactly by
	// one (mutex serializes updates); the clock never goes backwards.
	precise := c.Tick()
	if c.Tick() != precise+1 {
		t.Fatal("clock not monotonic with exact +1 after concurrent use")
	}
}
