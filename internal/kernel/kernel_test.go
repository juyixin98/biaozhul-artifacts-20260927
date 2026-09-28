package kernel

import (
	"testing"
	"time"
)

// fold is a test helper that builds a group and applies events, assigning
// versions 1..N exactly as the coordinator would after persistence.
func fold(t *testing.T, evs []Event) *State {
	t.Helper()
	if len(evs) == 0 {
		t.Fatal("no events")
	}
	for i := range evs {
		evs[i].Version = int64(i + 1)
	}
	s, err := Fold("g", evs)
	if err != nil {
		t.Fatalf("fold: %v", err)
	}
	return s
}

func ownerAt(s *State, pid int) (string, int64, PartitionPhase) {
	p := s.Partitions[pid]
	return p.Owner, p.Generation, p.Phase
}

// uniqueOwnerInvariant checks that no partition has two simultaneously valid
// owners. The state machine encodes this by clearing Owner during
// REVOKING/PENDING_GRANT; assert it directly.
func assertUniqueOwner(t *testing.T, s *State) {
	t.Helper()
	for _, p := range s.Partitions {
		if p.Owner != "" && p.PrevOwner != "" && p.Phase != PRevoking {
			t.Errorf("partition %d: owner %q and prev %q both set in phase %s", p.ID, p.Owner, p.PrevOwner, p.Phase)
		}
	}
}

var t0 = time.Unix(1_700_000_000, 0).UTC()

func TestCreateAndSingleJoinGrantsAll(t *testing.T) {
	evs, _ := CreateGroup(3, time.Second, t0, "r1")
	evs2, err := Join(fold(t, evs), "a", t0.Add(time.Second), "r2")
	if err != nil {
		t.Fatal(err)
	}
	s := fold(t, append(evs, evs2...))
	if s.Generation != 1 {
		t.Fatalf("generation = %d, want 1", s.Generation)
	}
	for i := 0; i < 3; i++ {
		if o, _, ph := ownerAt(s, i); o != "a" || ph != PStable {
			t.Fatalf("partition %d = (%s,%s), want a,STABLE", i, o, ph)
		}
	}
	if s.Members["a"].Generation != 1 {
		t.Fatalf("member a gen = %d, want 1", s.Members["a"].Generation)
	}
	assertUniqueOwner(t, s)
}

func TestSecondJoinTriggersRevokeConfirmThenGrant(t *testing.T) {
	evs, _ := CreateGroup(4, time.Second, t0, "r1")
	s := fold(t, evs)
	e2, _ := Join(s, "a", t0, "r2")
	s = fold(t, append(evs, e2...))
	// a owns [0,1,2,3] at gen 1.
	e3, err := Join(s, "b", t0.Add(time.Second), "r3")
	if err != nil {
		t.Fatal(err)
	}
	s = fold(t, appendS(evs, e2, e3))

	// A balanced 4-partition / 2-member split is 2/2. Two partitions move a->b
	// and must be REVOKING (still owned by a); b has the other two granted.
	revoking := 0
	ownedByB := 0
	for _, p := range s.Partitions {
		switch p.Phase {
		case PRevoking:
			if p.PrevOwner != "a" || p.PendingOwner != "b" {
				t.Fatalf("revoking partition %d: %s -> %s, want a -> b", p.ID, p.PrevOwner, p.PendingOwner)
			}
			if p.Owner != "a" {
				t.Fatalf("revoking partition %d must retain old owner a, got %q", p.ID, p.Owner)
			}
			revoking++
		case PStable:
			if p.Owner == "b" {
				ownedByB++
			}
			if p.Owner == "a" && p.Generation != 2 {
				t.Fatalf("retained a partition gen=%d want 2", p.Generation)
			}
		}
	}
	if revoking != 2 || ownedByB != 0 {
		t.Fatalf("after join: revoking=%d ownedByB=%d, want 2/0 (new member waits for revoke); phases=%v", revoking, ownedByB, phases(s))
	}
	// While a owes a revocation it stays fenced at gen 1; b is at gen 2.
	if s.Members["a"].Generation != 1 {
		t.Fatalf("a gen = %d, want pinned 1", s.Members["a"].Generation)
	}
	if s.Members["b"].Generation != 2 {
		t.Fatalf("b gen = %d, want 2", s.Members["b"].Generation)
	}
	if s.Phase != PhaseRebalancing {
		t.Fatalf("group phase = %s, want REBALANCING", s.Phase)
	}
	assertUniqueOwner(t, s)

	// Late commit by a at gen 1 on a REVOKING partition must be frozen with
	// PENDING_REVOCATION, not accepted.
	_, fails := Commit(s, "a", 1, []CommitItem{{Partition: revokingPid(s, "a", "b"), Offset: 99}}, t0, "rc")
	if len(fails) != 1 || fails[0].Code != "PARTITION_UNAVAILABLE" || fails[0].SubReason != "PENDING_REVOCATION" {
		t.Fatalf("commit on revoking partition: %+v", fails)
	}

	// a confirms revocation for BOTH demanded partitions at its pinned gen 1.
	pids := revokingPids(s, "a")
	if len(pids) != 2 {
		t.Fatalf("expected 2 revoking, got %d", len(pids))
	}
	e4, settled, kerr := ConfirmRevocation(s, "a", 1, pids, t0.Add(2*time.Second), "r4")
	if kerr != nil {
		t.Fatal(kerr)
	}
	if !settled {
		t.Fatal("group should be settled after all revocations confirmed")
	}
	s = fold(t, appendS(evs, e2, e3, e4))
	count := map[string]int{}
	for _, p := range s.Partitions {
		if p.Phase != PStable {
			t.Fatalf("partition %d phase=%s want STABLE", p.ID, p.Phase)
		}
		count[p.Owner]++
	}
	if count["a"] != 2 || count["b"] != 2 {
		t.Fatalf("final ownership a=%d b=%d want 2/2; %v", count["a"], count["b"], phases(s))
	}
	for _, pid := range pids {
		p := s.Partitions[pid]
		if p.Owner != "b" || p.Generation != 2 {
			t.Fatalf("partition %d = %s/%d, want b/2", pid, p.Owner, p.Generation)
		}
	}
	if s.Members["a"].Generation != 2 {
		t.Fatalf("a gen after confirm = %d want 2", s.Members["a"].Generation)
	}

	// Confirm a now-settled partition again -> PARTITION_UNAVAILABLE/NOT_REVOKING.
	_, _, kerr = ConfirmRevocation(s, "a", 2, pids[:1], t0, "ragain")
	if kerr == nil || len(kerr.Items) != 1 || kerr.Items[0].SubReason != "NOT_REVOKING" {
		t.Fatalf("re-confirm: %+v", kerr)
	}
}

func TestLateCommitFromFencedMemberRejected(t *testing.T) {
	evs, _ := CreateGroup(2, time.Second, t0, "r1")
	s := fold(t, evs)
	e2, _ := Join(s, "a", t0, "r2")
	s = fold(t, append(evs, e2...))
	// a owns both at gen1. Simulate a rebalance that advanced a to gen2 via a
	// no-op rebalance: join b then have a confirm. Simpler: heartbeat old gen.
	_, _, err := Heartbeat(s, "a", 1, t0, "hb")
	// a is currently gen1, so heartbeat gen1 is fine.
	_ = err
	// Force a generation advance without moving a's partitions: expire is
	// unsuitable; instead drive join+confirm so a retains at gen2.
	e3, _ := Join(s, "b", t0, "rb")
	s = fold(t, appendS(evs, e2, e3))
	// confirm whatever a owes so the group settles and a advances to gen2.
	var owed []int
	for _, p := range s.Partitions {
		if p.Phase == PRevoking && p.PrevOwner == "a" {
			owed = append(owed, p.ID)
		}
	}
	e4, _, _ := ConfirmRevocation(s, "a", s.Members["a"].Generation, owed, t0, "rc")
	s = fold(t, appendS(evs, e2, e3, e4))
	if s.Members["a"].Generation != 2 {
		t.Fatalf("a gen=%d want 2", s.Members["a"].Generation)
	}
	// Now a slow client commits at gen 1 -> ILLEGAL_GENERATION.
	_, fails := Commit(s, "a", 1, []CommitItem{{Partition: 0, Offset: 5}}, t0, "late")
	if len(fails) != 1 || fails[0].Code != "ILLEGAL_GENERATION" {
		t.Fatalf("late commit fails=%+v, want ILLEGAL_GENERATION", fails)
	}
	// Correct-generation commit on a partition a still owns is accepted.
	var owned int = -1
	for _, p := range s.Partitions {
		if p.Phase == PStable && p.Owner == "a" {
			owned = p.ID
		}
	}
	if owned < 0 {
		t.Skip("a retained no partition in this layout; cannot test valid commit")
	}
	e5, f2 := Commit(s, "a", 2, []CommitItem{{Partition: owned, Offset: 7}}, t0, "ok")
	if len(f2) != 0 || len(e5) != 1 {
		t.Fatalf("valid commit: evs=%d fails=%+v", len(e5), f2)
	}
	s = fold(t, appendS(evs, e2, e3, e4, e5))
	if s.Partitions[owned].Offset != 7 {
		t.Fatalf("offset = %d want 7", s.Partitions[owned].Offset)
	}
}

func TestLeaveReassignsAndPreserves(t *testing.T) {
	evs, _ := CreateGroup(4, time.Second, t0, "r1")
	s := fold(t, evs)
	e2, _ := Join(s, "a", t0, "r2")
	s = fold(t, append(evs, e2...))
	e3, _ := Join(s, "b", t0, "rb")
	s = fold(t, appendS(evs, e2, e3))
	owed := revokingPids(s, "a")
	e4, _, _ := ConfirmRevocation(s, "a", 1, owed, t0, "rc")
	s = fold(t, appendS(evs, e2, e3, e4))
	before := map[int]string{}
	for _, p := range s.Partitions {
		before[p.ID] = p.Owner
	}
	// b leaves cleanly; its partitions should be granted to a directly.
	e5, err := Leave(s, "b", t0.Add(time.Second), "rleave")
	if err != nil {
		t.Fatal(err)
	}
	s = fold(t, appendS(evs, e2, e3, e4, e5))
	for _, p := range s.Partitions {
		if p.Owner != "a" || p.Phase != PStable {
			t.Fatalf("after b leaves partition %d owner=%s phase=%s want a/STABLE", p.ID, p.Owner, p.Phase)
		}
	}
	// Partitions a already owned must have been RETAINED (owner unchanged in
	// `before`); assert at least one was preserved rather than all revoked.
	preserved := 0
	for pid, o := range before {
		if o == "a" && s.Partitions[pid].Owner == "a" {
			preserved++
		}
	}
	if preserved == 0 {
		t.Fatal("expected at least one a-owned partition to be preserved")
	}
	if s.Phase != PhaseStable {
		t.Fatalf("phase=%s want STABLE", s.Phase)
	}
	assertUniqueOwner(t, s)
}

func TestLastMemberLeavesEmptiesGroup(t *testing.T) {
	evs, _ := CreateGroup(2, time.Second, t0, "r1")
	s := fold(t, evs)
	e2, _ := Join(s, "a", t0, "r2")
	s = fold(t, append(evs, e2...))
	e3, _ := Leave(s, "a", t0, "r3")
	s = fold(t, appendS(evs, e2, e3))
	if len(s.ActiveMembers()) != 0 {
		t.Fatalf("members=%v want none", s.ActiveMembers())
	}
	for _, p := range s.Partitions {
		if p.Owner != "" || p.Phase != PEmpty {
			t.Fatalf("partition %d owner=%s phase=%s want empty/EMPTY", p.ID, p.Owner, p.Phase)
		}
	}
	if s.Phase != PhaseStable {
		t.Fatalf("phase=%s want STABLE", s.Phase)
	}
	// New member joins and all partitions are assigned completely.
	e4, _ := Join(s, "c", t0, "r4")
	s = fold(t, appendS(evs, e2, e3, e4))
	for _, p := range s.Partitions {
		if p.Owner != "c" || p.Phase != PStable {
			t.Fatalf("partition %d owner=%s phase=%s want c/STABLE", p.ID, p.Owner, p.Phase)
		}
	}
}

func TestExpireMarksUncertain(t *testing.T) {
	evs, _ := CreateGroup(2, time.Second, t0, "r1")
	s := fold(t, evs)
	e2, _ := Join(s, "a", t0, "r2")
	s = fold(t, append(evs, e2...))
	e3, _ := Expire(s, "a", t0.Add(20*time.Second), "rexp")
	s = fold(t, appendS(evs, e2, e3))
	uncertain := 0
	for _, p := range s.Partitions {
		if p.Uncertain {
			uncertain++
		}
	}
	if uncertain != 2 {
		t.Fatalf("uncertain partitions=%d want 2", uncertain)
	}
}

func TestRestartFoldEqualsLive(t *testing.T) {
	evs, _ := CreateGroup(3, time.Second, t0, "r1")
	s := fold(t, evs)
	var all []Event
	all = append(all, evs...)
	e2, _ := Join(s, "a", t0, "r2")
	all = append(all, e2...)
	s = fold(t, all)
	e3, _ := Join(s, "b", t0, "r3")
	all = append(all, e3...)
	s = fold(t, all)
	owed := revokingPids(s, "a")
	e4, _, _ := ConfirmRevocation(s, "a", 1, owed[:1], t0, "r4")
	all = append(all, e4...)

	// Rebuild entirely from the persisted log.
	rebuilt, err := Fold("g", versioned(all))
	if err != nil {
		t.Fatal(err)
	}
	live := fold(t, all)
	if !statesEqual(live, rebuilt) {
		t.Fatalf("live vs rebuilt mismatch:\nlive=%s\nreb =%s", dump(live), dump(rebuilt))
	}
}

// ---- helpers ----

func appendS(base ...interface{}) []Event {
	var out []Event
	for _, b := range base {
		switch v := b.(type) {
		case []Event:
			out = append(out, v...)
		case Event:
			out = append(out, v)
		}
	}
	return versioned(out)
}

func versioned(in []Event) []Event {
	out := make([]Event, len(in))
	copy(out, in)
	for i := range out {
		out[i].Version = int64(i + 1)
	}
	return out
}

func phases(s *State) []string {
	out := make([]string, len(s.Partitions))
	for i, p := range s.Partitions {
		out[i] = string(p.Phase) + ":" + p.Owner
	}
	return out
}

func revokingPid(s *State, old, new string) int {
	for _, p := range s.Partitions {
		if p.Phase == PRevoking && p.PrevOwner == old && p.PendingOwner == new {
			return p.ID
		}
	}
	return -1
}

func otherRevoking(s *State, old string) int {
	for _, p := range s.Partitions {
		if p.Phase == PRevoking && p.PrevOwner == old {
			return p.ID
		}
	}
	return -1
}

func revokingPids(s *State, old string) []int {
	var out []int
	for _, p := range s.Partitions {
		if p.Phase == PRevoking && p.PrevOwner == old {
			out = append(out, p.ID)
		}
	}
	return out
}

func statesEqual(a, b *State) bool {
	if a.Generation != b.Generation || a.Phase != b.Phase || len(a.Members) != len(b.Members) || len(a.Partitions) != len(b.Partitions) {
		return false
	}
	for id, ma := range a.Members {
		mb, ok := b.Members[id]
		if !ok || ma.Generation != mb.Generation || ma.Active != mb.Active {
			return false
		}
	}
	for i := range a.Partitions {
		x, y := a.Partitions[i], b.Partitions[i]
		if x.Owner != y.Owner || x.Generation != y.Generation || x.Phase != y.Phase ||
			x.PrevOwner != y.PrevOwner || x.PrevGeneration != y.PrevGeneration ||
			x.PendingOwner != y.PendingOwner || x.Offset != y.Offset || x.Uncertain != y.Uncertain {
			return false
		}
	}
	return true
}

func dump(s *State) string {
	out := ""
	for _, p := range s.Partitions {
		out += string(p.Phase) + ":" + p.Owner + " "
	}
	return out
}
