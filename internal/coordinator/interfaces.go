// Package coordinator runs the interrupt-budget reconciliation loop for
// voluntary evictions across local replica groups.
//
// Responsibilities:
//
//   - Observe the REAL readiness state through the cluster adapter and make
//     every budget calculation from that observation plus approved-but
//     -unfinished reservations persisted in SQLite.
//   - Approve a voluntary eviction and reserve its budget slot atomically:
//     admission is RE-EVALUATED inside the same immediate transaction that
//     inserts the approval, so concurrent requests cannot over-admit.
//   - Settle approvals against later observations (completion, involuntary
//     failure) and record an explicit reclaim confirmation when budget is
//     released. Reaping TTL-expired approvals and approvals invalidated by a
//     selector version change follows the same confirmation flow.
//   - Keep involuntary failures categorically separate: they settle
//     approvals without charging the voluntary request and are reported as
//     "rejected:involuntary-failure" / outcome "failed", never as something
//     the budget could have prevented.
package coordinator

import (
	"errors"
	"time"

	"evictor/internal/domain"
)

// Observer is the readiness adapter. The production wiring uses the synthetic
// local cluster; anything returning a freshness-bounded Observation works.
type Observer interface {
	Observe(group string, maxAge time.Duration) domain.Observation
}

// ErrStaleObservation maps to an "undecidable" answer: the adapter sample is
// older than the configured freshness budget. The coordinator neither
// approves nor rejects on stale data.
var ErrStaleObservation = errors.New("coordinator: observation stale")

// Clock is injectable so the reconciliation loop and TTL tests are
// deterministic.
type Clock interface{ Now() time.Time }

type funcClock struct{ f func() time.Time }

func (c funcClock) Now() time.Time { return c.f() }
