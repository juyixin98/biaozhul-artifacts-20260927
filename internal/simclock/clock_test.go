package simclock_test

import (
	"testing"

	"igmpq/internal/simclock"
)

func TestManualAdvance(t *testing.T) {
	c := simclock.NewManual(100)
	if c.Now() != 100 {
		t.Fatalf("now=%d, want 100", c.Now())
	}
	c.AdvanceTo(250)
	c.AdvanceTo(250) // no-op
	if c.Now() != 250 {
		t.Fatalf("now=%d, want 250", c.Now())
	}
}

func TestManualBackwardsPanics(t *testing.T) {
	c := simclock.NewManual(100)
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic on backwards advance")
		}
	}()
	c.AdvanceTo(99)
}

func TestNegativeStartPanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic on negative start")
		}
	}()
	simclock.NewManual(-1)
}
