package protocol

import "sync"

// Clock is a Lamport logical clock (L. Lamport, 1978). It is the only
// time ordering used inside snapshots; wall clocks are cosmetic.
type Clock struct {
	mu sync.Mutex
	t  int64
}

func NewClock() *Clock { return &Clock{} }

// Tick advances for a local event and returns the new value.
func (c *Clock) Tick() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t++
	return c.t
}

// Observe merges a remote timestamp on receive: max(local, remote) + 1.
func (c *Clock) Observe(remote int64) int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	if remote > c.t {
		c.t = remote
	}
	c.t++
	return c.t
}

// Get returns the current value without advancing it.
func (c *Clock) Get() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}
