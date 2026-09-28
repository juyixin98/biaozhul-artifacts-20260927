package nat

import (
	"testing"
	"time"
)

func TestMonotonicClock_NoRewind(t *testing.T) {
	t0 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	c := NewClock(t0)
	if got := c.Now(); !got.Equal(t0) {
		t.Fatalf("initial now = %v, want %v", got, t0)
	}
	now, rewind := c.Advance(t0.Add(10 * time.Second))
	if rewind || !now.Equal(t0.Add(10*time.Second)) {
		t.Fatalf("advance forward: now=%v rewind=%v", now, rewind)
	}
	// Older timestamp must not move the clock back.
	now, rewind = c.Advance(t0.Add(5 * time.Second))
	if !rewind {
		t.Fatalf("older timestamp must report rewind")
	}
	if !now.Equal(t0.Add(10 * time.Second)) {
		t.Fatalf("clock moved backwards: now=%v", now)
	}
	// Equal timestamp: no rewind, no change.
	now, rewind = c.Advance(t0.Add(10 * time.Second))
	if rewind || !now.Equal(t0.Add(10*time.Second)) {
		t.Fatalf("equal timestamp: now=%v rewind=%v", now, rewind)
	}
}
