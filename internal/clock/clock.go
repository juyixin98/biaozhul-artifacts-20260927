// Package clock abstracts time so that visibility-timeout races are
// deterministic in tests. Production code uses System; tests use Fake and
// advance it explicitly, which makes "extend vs timeout" decidable rather than
// racy.
package clock

import "time"

// Clock is the narrow time source used by kernel and stores.
type Clock interface {
	Now() time.Time
}

// System reads the wall clock.
type System struct{}

// Now returns the current wall-clock time.
func (System) Now() time.Time { return time.Now().UTC() }

// Fake is a controllable clock for tests.
type Fake struct{ T time.Time }

// NewFake returns a Fake clock seeded at a fixed instant.
func NewFake() *Fake {
	return &Fake{T: time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)}
}

// Now returns the faked time.
func (f *Fake) Now() time.Time { return f.T }

// Advance moves the faked clock forward by d.
func (f *Fake) Advance(d time.Duration) { f.T = f.T.Add(d) }
