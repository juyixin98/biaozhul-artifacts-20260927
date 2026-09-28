// Package clock implements a Lamport logical clock (Lamport, 1978).
//
// The service models reliable FIFO channels between three local processes.
// Every application message (transfer) and every Chandy-Lamport marker
// carries a Lamport timestamp. Local events use tick() = t+1; receiving an
// event with remote timestamp t' uses merge() = max(t, t') + 1.
//
// The clock itself is only used for ordering and for logging; snapshot
// correctness depends on the FIFO marker rule, not on wall-clock time.
package clock

import "sync"

// Clock is a concurrency-safe Lamport clock.
type Clock struct {
	mu sync.Mutex
	t  uint64
}

// New returns a clock starting at 0 (the pre-initial value; the first tick is 1).
func New() *Clock { return &Clock{} }

// Tick advances for a local event and returns the new value.
func (c *Clock) Tick() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t++
	return c.t
}

// Observe merges a remote timestamp ("happens-before" merge rule) and returns
// the new value: t = max(t, observed) + 1.
func (c *Clock) Observe(observed uint64) uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	if observed > c.t {
		c.t = observed
	}
	c.t++
	return c.t
}

// Peek returns the current value without advancing.
func (c *Clock) Peek() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

// Compare implements the Lamport happens-before comparison only in terms of
// timestamps; callers that need a total order must break ties by node id.
func Compare(a uint64, b uint64) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	default:
		return 0
	}
}
