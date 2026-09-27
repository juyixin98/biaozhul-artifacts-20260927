// Package simclock provides the injected clock used by the membership
// engine. The engine never reads wall time; all timing is driven by the
// caller through Manual, which makes replays deterministic and lets tests
// hit exact timer boundaries.
package simclock

import "fmt"

// Clock is the minimal time source the engine depends on.
// Now returns milliseconds since an arbitrary epoch.
type Clock interface {
	Now() int64
}

// Manual is a Clock advanced explicitly by the caller. It is monotonic:
// advancing backwards is a programming error and panics.
type Manual struct {
	now int64
}

// NewManual returns a Manual clock starting at startMS.
func NewManual(startMS int64) *Manual {
	if startMS < 0 {
		panic(fmt.Sprintf("simclock: negative start %d", startMS))
	}
	return &Manual{now: startMS}
}

// Now implements Clock.
func (m *Manual) Now() int64 { return m.now }

// AdvanceTo moves the clock forward to t (milliseconds). Advancing to the
// current time is a no-op; going backwards panics.
func (m *Manual) AdvanceTo(t int64) {
	if t < m.now {
		panic(fmt.Sprintf("simclock: cannot move clock backwards from %d to %d", m.now, t))
	}
	m.now = t
}
