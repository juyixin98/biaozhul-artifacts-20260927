package nat

import "time"

// MonotonicClock is a high-water-mark clock driven by observed packet
// timestamps. Its effective time never moves backwards: advancing with an
// older timestamp reports a rewind but leaves Now() unchanged, so an expired
// mapping cannot be revived by packets carrying an old clock.
type MonotonicClock struct {
	highWater time.Time
}

// NewClock starts the clock at t (the first observed timestamp).
func NewClock(t time.Time) *MonotonicClock { return &MonotonicClock{highWater: t} }

// Advance sets the high-water mark to max(highWater, t). rewind is true only
// when t is strictly older than an already-established high-water mark. The
// first timestamp of a run (highWater still zero) initializes the clock and is
// never reported as a rewind, regardless of whether it equals another run's
// epoch.
func (c *MonotonicClock) Advance(t time.Time) (now time.Time, rewind bool) {
	if c.highWater.IsZero() {
		c.highWater = t
		return t, false
	}
	if t.After(c.highWater) {
		c.highWater = t
	} else if t.Before(c.highWater) {
		rewind = true
	}
	return c.highWater, rewind
}

// Now returns the current high-water mark.
func (c *MonotonicClock) Now() time.Time { return c.highWater }
