// Package clock provides the injectable simulation clock. Time only moves
// forward and only when Advanced — nothing here reads a wall clock, which
// makes replay deterministic and reproducible.
package clock

import (
	"fmt"
	"sync"

	"igmpv2timer/internal/model"
)

// Clock is a monotonic virtual clock in milliseconds.
type Clock struct {
	mu sync.RWMutex
	t  model.Millis
}

// New returns a clock anchored at the replay epoch (0ms).
func New() *Clock { return &Clock{} }

// Now returns the current virtual time.
func (c *Clock) Now() model.Millis {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.t
}

// Advance moves the clock to t. Backward movement is rejected (it would
// break timer-generation semantics).
func (c *Clock) Advance(t model.Millis) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if t < c.t {
		return fmt.Errorf("clock cannot move backwards: now=%d requested=%d", c.t, t)
	}
	c.t = t
	return nil
}
