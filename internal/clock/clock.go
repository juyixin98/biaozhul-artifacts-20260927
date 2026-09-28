// Package clock defines the time source used by every layer, so tests can run
// deterministically without sleeps while production uses wall-clock time.
package clock

import "time"

// Clock is the minimal time surface the broker needs.
type Clock interface {
	Now() time.Time
}

// Real is the wall-clock Clock.
type Real struct{}

// NewReal returns a wall-clock clock.
func NewReal() Real { return Real{} }

// Now returns the current wall-clock time.
func (Real) Now() time.Time { return time.Now().UTC() }

// Fake is a manually controlled clock for deterministic tests.
type Fake struct {
	t time.Time
}

// NewFake returns a Fake clock anchored at t (UTC-normalized).
func NewFake(t time.Time) *Fake {
	return &Fake{t: t.UTC()}
}

// Now returns the fake current time.
func (f *Fake) Now() time.Time { return f.t }

// Advance moves the clock forward by d.
func (f *Fake) Advance(d time.Duration) { f.t = f.t.Add(d) }

// Set moves the clock to an absolute time (UTC-normalized).
func (f *Fake) Set(t time.Time) { f.t = t.UTC() }
