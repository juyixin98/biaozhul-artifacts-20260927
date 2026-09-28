// Package budget computes interruptibility snapshots from real readiness
// observations, recorded involuntary failures and charged eviction
// reservations. The computation is a pure function of its inputs, which is what
// lets the tests assert exact numbers.
package budget

import (
	"github.com/local/evictioncoordinator/internal/domain"
)

// InstanceState is the reconciled view of one member at decision time.
type InstanceState struct {
	InstanceID string
	Ready      bool
	Observed   bool // a current-epoch readiness observation exists
	Failed     bool // involuntary failure recorded (current epoch)
	// LiveApproval is a PENDING approval for this instance at the current epoch.
	LiveApproval *domain.Approval
	// ChargedApproval is any reservation still consuming budget for this
	// instance at the current epoch: pending, expired or stale, provided it has
	// not been explicitly reclaimed. A pending approval appears in both fields.
	ChargedApproval *domain.Approval
}

// Compute is the single source of truth for "may one more voluntary eviction
// run right now?". It deliberately takes the reconciled states as explicit
// inputs rather than reading storage itself: the coordinator loads them inside
// its transaction and the same function is used by the tests with fixtures.
//
// What counts as currently unavailable (each member counted exactly once):
//   - observed not-ready;
//   - involuntary failure recorded (a fact, never something a budget "denied");
//   - observed ready but holding a charged reservation (pending approval, or an
//     expired/stale approval not yet reclaimed) — the slot was taken when
//     approved and remains charged until an explicit reclamation, even while
//     the member is still reported ready ("approved, then paused");
//   - unknown (no current-epoch observation) is NOT counted as unavailable;
//     instead it poisons freshness so the answer is "cannot decide".
//
// legacyCharged counts reservations from PREVIOUS selector epochs that have not
// been reclaimed. They can no longer be attached to a current member (the
// selector moved), yet the group budget must keep paying for them until an
// explicit reclamation — otherwise a selector bump would silently refund slots.
func Compute(g domain.Group, states []InstanceState, legacyCharged int32) domain.BudgetSnapshot {
	snap := domain.BudgetSnapshot{
		Group:           g.Key(),
		SelectorEpoch:   g.SelectorEpoch,
		DesiredReplicas: g.Replicas,
	}

	var ready, unavailable, charged int32
	var unknown, failed int
	for _, s := range states {
		switch {
		case s.Failed:
			failed++
			unavailable++
		case !s.Observed:
			unknown++
		case !s.Ready:
			unavailable++
		default:
			ready++
			if s.ChargedApproval != nil {
				// Still reported ready, but the reservation holds the slot.
				unavailable++
			}
		}
		if s.ChargedApproval != nil {
			charged++
		}
	}
	// Legacy reservations occupy group-level slots even though their member is
	// not attributable under the current selector.
	unavailable += legacyCharged
	charged += legacyCharged

	snap.ReadyReplicas = ready
	snap.CurrentUnavailable = unavailable
	snap.UnknownReplicas = int32(unknown)
	snap.FailedReplicas = int32(failed)
	snap.ChargedApprovals = charged

	base := int32(len(states))
	if g.Replicas > base {
		base = g.Replicas
	}
	snap.AllowedUnavailable = allowedUnavailable(g, base)

	if unavailable <= snap.AllowedUnavailable {
		snap.AvailableSlots = snap.AllowedUnavailable - unavailable
	}
	snap.Fresh = unknown == 0
	return snap
}

// allowedUnavailable converts both budget modes into a maximum-unavailable
// ceiling using total as the percentage base.
func allowedUnavailable(g domain.Group, total int32) int32 {
	switch g.BudgetMode {
	case domain.MaxUnavailable:
		if g.BudgetPercent > 0 {
			return ceilPercent(total, g.BudgetPercent)
		}
		if g.BudgetValue < total {
			return g.BudgetValue
		}
		return total
	case domain.MinAvailable:
		minAvail := g.BudgetValue
		if g.BudgetPercent > 0 {
			minAvail = ceilPercent(total, g.BudgetPercent)
		}
		if minAvail > total {
			minAvail = total
		}
		return total - minAvail
	}
	return 0
}

// ceilPercent rounds UP, the conservative direction for both budget modes:
// maxUnavailable 25% of 3 -> 1; minAvailable 75% of 3 -> 3 available required
// (ceil 2.25 = 3).
func ceilPercent(total, percent int32) int32 {
	return int32((int64(total)*int64(percent) + 99) / 100)
}

// CanApprove is the guard used by the coordinator transaction.
func CanApprove(snap domain.BudgetSnapshot, target InstanceState) bool {
	if !snap.Fresh {
		return false
	}
	if target.Failed {
		return false
	}
	// A pending approval blocks a duplicate request on the same instance. An
	// expired/stale reservation does not block by identity — but it still
	// charges the group budget, which the slot check below enforces.
	if target.LiveApproval != nil {
		return false
	}
	return snap.AvailableSlots >= 1
}
