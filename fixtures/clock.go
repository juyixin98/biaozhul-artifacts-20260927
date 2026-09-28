// Package fixtures provides local, deterministic test fixtures: a
// controllable clock, synthetic member behavior (including a member that
// withholds revocation confirmations) and scenario definitions. Nothing here
// talks to a real broker or external service.
package fixtures

import (
	"sync"
	"time"
)

// Clock is a controllable time source.
type Clock struct {
	mu  sync.Mutex
	now time.Time
}

// NewClock starts at a fixed, non-zero instant for reproducible tests.
func NewClock() *Clock {
	t, _ := time.Parse(time.RFC3339, "2026-01-01T00:00:00Z")
	return &Clock{now: t}
}

// Now returns the current fake time.
func (c *Clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// Advance moves the clock forward and returns the new time.
func (c *Clock) Advance(d time.Duration) time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	return c.now
}

// Set moves the clock to an absolute time.
func (c *Clock) Set(t time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = t
}
