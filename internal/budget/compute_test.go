package budget_test

import (
	"testing"

	"github.com/local/evictioncoordinator/internal/budget"
	"github.com/local/evictioncoordinator/internal/domain"
)

func g(mode domain.BudgetMode, value, percent int32, replicas int32) domain.Group {
	return domain.Group{
		Name: "web", Namespace: "default", Replicas: replicas,
		BudgetMode: mode, BudgetValue: value, BudgetPercent: percent,
		SelectorLabels: map[string]string{"app": "web"}, SelectorEpoch: 1,
	}
}

func readyStates(n int, pendingApproval ...int) []budget.InstanceState {
	states := make([]budget.InstanceState, n)
	for i := range states {
		states[i] = budget.InstanceState{InstanceID: string(rune('a' + i)), Observed: true, Ready: true}
	}
	for _, idx := range pendingApproval {
		a := &domain.Approval{ID: "appr" + string(rune('a'+idx)), State: domain.ApprovalPending}
		states[idx].LiveApproval = a
		states[idx].ChargedApproval = a
	}
	return states
}

func TestCompute_MaxUnavailableOneOfThree(t *testing.T) {
	// maxUnavailable=1, 3 ready => exactly one slot.
	snap := budget.Compute(g(domain.MaxUnavailable, 1, 0, 3), readyStates(3), 0)
	if snap.AllowedUnavailable != 1 {
		t.Fatalf("allowed=%d want 1", snap.AllowedUnavailable)
	}
	if snap.AvailableSlots != 1 || !snap.Fresh {
		t.Fatalf("slots=%d fresh=%v, want 1/true", snap.AvailableSlots, snap.Fresh)
	}
}

func TestCompute_PendingApprovalConsumesSlotWhileStillReady(t *testing.T) {
	// Approved-then-paused: member is still observed ready but holds a pending
	// approval. Its slot must stay charged.
	states := readyStates(3, 0)
	snap := budget.Compute(g(domain.MaxUnavailable, 1, 0, 3), states, 0)
	if snap.ChargedApprovals != 1 {
		t.Fatalf("charged=%d want 1", snap.ChargedApprovals)
	}
	if snap.CurrentUnavailable != 1 {
		t.Fatalf("unavailable=%d want 1 (approval charges the slot even while ready)",
			snap.CurrentUnavailable)
	}
	if snap.AvailableSlots != 0 {
		t.Fatalf("slots=%d want 0: no second eviction may be approved", snap.AvailableSlots)
	}
}

func TestCompute_NotReadyAndApprovalNotDoubleCounted(t *testing.T) {
	// One member not-ready AND carrying a pending approval must be counted once.
	states := readyStates(3)
	states[1].Ready = false
	states[1].LiveApproval = &domain.Approval{ID: "x", State: domain.ApprovalPending}
	snap := budget.Compute(g(domain.MaxUnavailable, 2, 0, 3), states, 0)
	if snap.CurrentUnavailable != 1 {
		t.Fatalf("unavailable=%d want 1 (no double count)", snap.CurrentUnavailable)
	}
	if snap.AvailableSlots != 1 {
		t.Fatalf("slots=%d want 1", snap.AvailableSlots)
	}
}

func TestCompute_MinAvailableSemantics(t *testing.T) {
	// minAvailable=2 of 3 => at most 1 unavailable.
	snap := budget.Compute(g(domain.MinAvailable, 2, 0, 3), readyStates(3), 0)
	if snap.AllowedUnavailable != 1 || snap.AvailableSlots != 1 {
		t.Fatalf("allowed=%d slots=%d want 1/1", snap.AllowedUnavailable, snap.AvailableSlots)
	}

	// minAvailable 75% rounds UP: 3 * .75 = 2.25 -> 3 required -> 0 slots.
	snapPct := budget.Compute(g(domain.MinAvailable, 0, 75, 3), readyStates(3), 0)
	if snapPct.AllowedUnavailable != 0 {
		t.Fatalf("pct allowed=%d want 0 (round up conservatively)", snapPct.AllowedUnavailable)
	}

	// maxUnavailable 25% of 3 rounds UP to 1.
	snapMaxPct := budget.Compute(g(domain.MaxUnavailable, 0, 25, 3), readyStates(3), 0)
	if snapMaxPct.AllowedUnavailable != 1 {
		t.Fatalf("maxunavail pct allowed=%d want 1", snapMaxPct.AllowedUnavailable)
	}
}

func TestCompute_UnknownReadinessIsStaleNotOptimistic(t *testing.T) {
	// Two observed ready, one never observed at the current epoch.
	states := readyStates(3)
	states[2].Observed = false
	snap := budget.Compute(g(domain.MaxUnavailable, 1, 0, 3), states, 0)
	if snap.Fresh {
		t.Fatal("fresh=true want false with an unknown member")
	}
	if snap.UnknownReplicas != 1 {
		t.Fatalf("unknown=%d want 1", snap.UnknownReplicas)
	}
	// Even though arithmetic would show a slot, CanApprove must refuse.
	if budget.CanApprove(snap, states[0]) {
		t.Fatal("CanApprove must be false when snapshot is not fresh")
	}
}

func TestCompute_FailedMemberCountsButCannotBeApproved(t *testing.T) {
	states := readyStates(3)
	states[0].Failed = true
	snap := budget.Compute(g(domain.MaxUnavailable, 1, 0, 3), states, 0)
	// The failure consumes real availability (2 ready of 3).
	if snap.FailedReplicas != 1 || snap.ReadyReplicas != 2 {
		t.Fatalf("failed=%d ready=%d want 1/2", snap.FailedReplicas, snap.ReadyReplicas)
	}
	if snap.AvailableSlots != 0 {
		t.Fatalf("slots=%d want 0: failure already consumed the disruption budget",
			snap.AvailableSlots)
	}
	// And approving the failed member itself is refused by the guard.
	if budget.CanApprove(snap, states[0]) {
		t.Fatal("must not approve an eviction for an already-failed member")
	}
}
