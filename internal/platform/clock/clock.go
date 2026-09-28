// Package clock provides the time abstraction used by the controller.
// Tests inject a deterministic step clock so DeletionTimestamp values
// and log timestamps are stable across runs.
package clock

import (
	"sync/atomic"
	"time"
)

// Clock is the subset of time the controller needs.
type Clock interface {
	Now() time.Time
}

// WallClock returns the real wall clock.
type WallClock struct{}

// Now returns the current time with second granularity trimmed to
// milliseconds to keep deterministic JSON round trips stable.
func (WallClock) Now() time.Time {
	return time.Now().UTC().Truncate(time.Millisecond)
}

// StepClock is a deterministic clock: time starts at a fixed instant and
// only advances when Advance is called (the controller calls it once
// per reconcile tick in deterministic mode). Safe for concurrent use.
type StepClock struct {
	step atomic.Int64
	base time.Time
}

// NewStepClock creates a step clock at base.
func NewStepClock(base time.Time) *StepClock {
	if base.IsZero() {
		base = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	}
	return &StepClock{base: base}
}

// Now returns base + step*millisecond.
func (c *StepClock) Now() time.Time {
	return c.base.Add(time.Duration(c.step.Load()) * time.Millisecond)
}

// Advance moves the clock by one millisecond and returns the new time.
func (c *StepClock) Advance() time.Time {
	n := c.step.Add(1)
	return c.base.Add(time.Duration(n) * time.Millisecond)
}

// Set forces the step counter (used by fixtures).
func (c *StepClock) Set(n int64) { c.step.Store(n) }
