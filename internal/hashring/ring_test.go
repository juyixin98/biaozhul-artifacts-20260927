package hashring

import (
	"fmt"
	"sort"
	"testing"

	"flexhash/internal/fherr"
)

func m(id string, w int) Member {
	return Member{ID: id, Address: "127.0.0.1:0", Weight: w, Healthy: true}
}

func TestAssignFillsAllBucketsAndMeetsQuota(t *testing.T) {
	const B = 1024
	mgr := NewManager(B)
	ring, _, err := mgr.Bootstrap(1, []Member{m("a", 3), m("b", 2), m("c", 1)}, 0)
	if err != nil {
		t.Fatalf("bootstrap: %v", err)
	}
	q := ring.Quota()
	if q["a"] != 512 || q["b"] != 341 || q["c"] != 171 {
		t.Fatalf("quota concrete = %v, want a=512 b=341 c=171 (1024*3/6=512, 1024*2/6=341.33, 1024/6=170.67->remainder c)", q)
	}
	owned := map[string]int{}
	for b := 0; b < B; b++ {
		owned[ring.Owner(b)]++
	}
	if len(owned) != 3 || owned["a"]+owned["b"]+owned["c"] != B {
		t.Fatalf("ownership counts wrong: %v", owned)
	}
}

func TestFlowStableWhileMembersUnchanged(t *testing.T) {
	const B = 1024
	mgr := NewManager(B)
	ring, _, _ := mgr.Bootstrap(1, []Member{m("a", 1), m("b", 1), m("c", 1)}, 0)
	keys := []string{"flow|10.0.0.1|1234|10.1.0.1|80|tcp", "k2", "k3", "k4", "k5"}
	first := map[string]Decision{}
	for _, k := range keys {
		d := ResolveOn(ring, 0, k)
		first[k] = d
	}
	// Re-applying the same member set (e.g. an idempotent config push that
	// does change version) must keep every flow on the same member.
	ring2, moved, err := mgr.ApplyConfig(2, []Member{m("a", 1), m("b", 1), m("c", 1)})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if len(moved) != 0 {
		t.Fatalf("unchanged membership moved %d buckets: %v", len(moved), moved[:min(10, len(moved))])
	}
	for _, k := range keys {
		d := ResolveOn(ring2, 0, k)
		if d.Chosen != first[k].Chosen || d.Bucket != first[k].Bucket {
			t.Fatalf("flow %q moved: %+v vs %+v", k, d, first[k])
		}
	}
}

func TestAddMemberMovesOnlyDonatedBuckets(t *testing.T) {
	const B = 1024
	mgr := NewManager(B)
	ring1, _, _ := mgr.Bootstrap(1, []Member{m("a", 1), m("b", 1), m("c", 1)}, 0)

	ring2, moved, err := mgr.ApplyConfig(2,
		[]Member{m("a", 1), m("b", 1), m("c", 1), m("d", 1)})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	// New member d gets 256 buckets (1024/4); every moved bucket must land
	// on d, and no bucket belonging to an unchanged survivor should move
	// between survivors.
	if len(moved) != ring2.Quota()["d"] {
		t.Fatalf("moved %d, want exactly d's quota %d", len(moved), ring2.Quota()["d"])
	}
	for _, b := range moved {
		if ring1.Owner(b) == ring2.Owner(b) {
			t.Fatalf("bucket %d listed moved but owner unchanged", b)
		}
		if ring2.Owner(b) != "d" {
			t.Fatalf("bucket %d moved to %q instead of new member d", b, ring2.Owner(b))
		}
	}
	// Every non-moved bucket keeps its owner.
	movedSet := map[int]bool{}
	for _, b := range moved {
		movedSet[b] = true
	}
	for b := 0; b < B; b++ {
		if !movedSet[b] && ring1.Owner(b) != ring2.Owner(b) {
			t.Fatalf("bucket %d changed although not in moved list (%s -> %s)",
				b, ring1.Owner(b), ring2.Owner(b))
		}
	}
}

func TestRemoveMemberMovesOnlyItsBuckets(t *testing.T) {
	const B = 1024
	mgr := NewManager(B)
	ring1, _, _ := mgr.Bootstrap(1,
		[]Member{m("a", 1), m("b", 1), m("c", 1), m("d", 1)}, 0)
	cQ1 := ring1.Quota()["c"]

	ring2, moved, err := mgr.ApplyConfig(2, []Member{m("a", 1), m("b", 1), m("d", 1)})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	// Every moved bucket must have belonged to c; survivors a,b,d never lose
	// a bucket to each other (their per-member quotas grow proportionally but
	// only by receiving c's buckets).
	for _, b := range moved {
		if ring1.Owner(b) != "c" {
			t.Fatalf("bucket %d moved although owner %q survives", b, ring1.Owner(b))
		}
		if ring2.Owner(b) == "" {
			t.Fatalf("bucket %d unassigned after removal", b)
		}
	}
	if len(moved) != cQ1 {
		t.Fatalf("moved %d want c's %d", len(moved), cQ1)
	}
	q2 := ring2.Quota()
	if q2["a"]+q2["b"]+q2["d"] != B || q2["a"] < 1 || q2["b"] < 1 || q2["d"] < 1 {
		t.Fatalf("post-remove quota wrong: %v", q2)
	}
}

func TestWeightChangeMinimalMigration(t *testing.T) {
	const B = 1024
	mgr := NewManager(B)
	ring1, _, _ := mgr.Bootstrap(1, []Member{m("a", 1), m("b", 1)}, 0)
	q1 := ring1.Quota() // 512/512

	ring2, moved, err := mgr.ApplyConfig(2, []Member{m("a", 3), m("b", 1)})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	q2 := ring2.Quota() // 768/256
	if q2["a"] != 768 || q2["b"] != 256 {
		t.Fatalf("quota after reweight = %v want 768/256", q2)
	}
	// Exactly |delta| buckets move: b shrank by 256.
	if len(moved) != abs(q2["a"]-q1["a"]) {
		t.Fatalf("moved %d, want exactly 256", len(moved))
	}
	for _, b := range moved {
		if ring1.Owner(b) != "b" || ring2.Owner(b) != "a" {
			t.Fatalf("bucket %d moved %s->%s, want only b->a",
				b, ring1.Owner(b), ring2.Owner(b))
		}
	}
}

func TestZeroWeightMemberReceivesNoTraffic(t *testing.T) {
	const B = 1024
	mgr := NewManager(B)
	ring, _, err := mgr.Bootstrap(1,
		[]Member{m("a", 1), m("b", 1), m("z", 0)}, 0)
	if err != nil {
		t.Fatalf("bootstrap: %v", err)
	}
	if q := ring.Quota(); q["z"] != 0 || q["a"]+q["b"] != B {
		t.Fatalf("zero-weight member quota = %d, want 0 (all=%v)", q["z"], q)
	}
	for i := 0; i < B; i++ {
		if ring.Owner(i) == "z" {
			t.Fatalf("zero-weight member owns bucket %d", i)
		}
	}
	// Direct resolutions must never select z.
	for i := 0; i < 2000; i++ {
		d := ResolveOn(ring, 0, fmt.Sprintf("flow-%d", i))
		if d.Chosen == "z" {
			t.Fatalf("flow %d routed to zero-weight z", i)
		}
	}
}

func TestAllZeroWeightsRejected(t *testing.T) {
	mgr := NewManager(64)
	_, _, err := mgr.Bootstrap(1, []Member{m("a", 0), m("b", 0)}, 0)
	if fherr.KindOf(err) != fherr.KindInput {
		t.Fatalf("bootstrap all-zero kind=%v err=%v, want input_error", fherr.KindOf(err), err)
	}
}

func TestAllDownIsUnavailable(t *testing.T) {
	const B = 128
	mgr := NewManager(B)
	_, _, _ = mgr.Bootstrap(1, []Member{m("a", 1), m("b", 1)}, 0)
	if _, err := mgr.SetHealth("a", false); err != nil {
		t.Fatalf("set a down: %v", err)
	}
	if _, err := mgr.SetHealth("b", false); err != nil {
		t.Fatalf("set b down: %v", err)
	}
	d, err := mgr.Resolve("some-flow")
	if err != nil {
		t.Fatalf("resolve with all down returned err %v; want decision with empty chosen", err)
	}
	if d.Chosen != "" {
		t.Fatalf("all down selected %q", d.Chosen)
	}
}

func TestFailoverAndVersionedRecovery(t *testing.T) {
	const B = 128
	mgr := NewManager(B)
	ring, _, _ := mgr.Bootstrap(1, []Member{m("a", 3), m("b", 1)}, 0)

	// Find flows structurally owned by a.
	var aFlow string
	for i := 0; ; i++ {
		k := fmt.Sprintf("flow-%d", i)
		if ring.BucketOf(k) >= 0 {
			d := ResolveOn(ring, 0, k)
			if d.Owner == "a" {
				aFlow = k
				break
			}
		}
		if i > 100000 {
			t.Fatal("could not find a-owned flow")
		}
	}
	before := ResolveOn(ring, 0, aFlow)
	if before.Chosen != "a" || before.Failover {
		t.Fatalf("precondition wrong: %+v", before)
	}

	// Immediate exclusion: marking a down must reroute at once.
	rev, err := mgr.SetHealth("a", false)
	if err != nil || rev != 1 {
		t.Fatalf("SetHealth a: rev=%d err=%v", rev, err)
	}
	down, hrevD, _ := mgr.Snapshot()
	dDuring := ResolveOn(down, hrevD, aFlow)
	if dDuring.Chosen != "b" || !dDuring.Failover || dDuring.Owner != "a" {
		t.Fatalf("failover wrong: %+v", dDuring)
	}

	// Redundant transition is a state conflict, not a new revision.
	if _, err := mgr.SetHealth("a", false); fherr.KindOf(err) != fherr.KindStateConflict {
		t.Fatalf("repeat down kind=%v, want state_conflict", fherr.KindOf(err))
	}

	// Recovery is versioned: explicit new health revision restores ownership.
	rev2, err := mgr.SetHealth("a", true)
	if err != nil || rev2 != 2 {
		t.Fatalf("SetHealth a up: rev=%d err=%v", rev2, err)
	}
	up, hrev, _ := mgr.Snapshot()
	dAfter := ResolveOn(up, hrev, aFlow)
	if dAfter.Chosen != "a" || dAfter.Failover {
		t.Fatalf("recovery wrong: %+v", dAfter)
	}
	if hrev != 2 {
		t.Fatalf("health revision after recovery = %d, want 2", hrev)
	}
}

func TestVersionConflictsRejected(t *testing.T) {
	mgr := NewManager(16)
	_, _, _ = mgr.Bootstrap(1, []Member{m("a", 1)}, 0)
	// v3 skipping v2 -> state conflict.
	if _, _, err := mgr.ApplyConfig(3, []Member{m("a", 1), m("b", 1)}); fherr.KindOf(err) != fherr.KindStateConflict {
		t.Fatalf("skip version kind=%v err=%v", fherr.KindOf(err), err)
	}
	// Unknown member health change -> input error.
	if _, err := mgr.SetHealth("ghost", false); fherr.KindOf(err) != fherr.KindInput {
		t.Fatalf("unknown member kind=%v", fherr.KindOf(err))
	}
	// Double bootstrap -> conflict.
	if _, _, err := mgr.Bootstrap(1, []Member{m("a", 1)}, 0); fherr.KindOf(err) != fherr.KindStateConflict {
		t.Fatalf("double bootstrap kind=%v", fherr.KindOf(err))
	}
}

func TestResolvedOwnersSortedDeterministic(t *testing.T) {
	// Tie-break determinism: weights 1:1:1 and a tiny bucket count exercises
	// equal-remainder ordering; assignments must be stable across builds.
	const B = 64
	first := ownersOf(NewManagerBootstrapped(t, []Member{m("a", 1), m("b", 1), m("c", 1), m("d", 1)}, B))
	for i := 0; i < 20; i++ {
		again := ownersOf(NewManagerBootstrapped(t,
			[]Member{m("d", 1), m("c", 1), m("b", 1), m("a", 1)}, B))
		if !sliceEqual(first, again) {
			t.Fatalf("nondeterministic ownership:\n%v\n%v", first, again)
		}
	}
}

func NewManagerBootstrapped(t *testing.T, members []Member, b int) *Ring {
	t.Helper()
	sort.Slice(members, func(i, j int) bool { return members[i].ID < members[j].ID })
	mgr := NewManager(b)
	r, _, err := mgr.Bootstrap(1, members, 0)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func ownersOf(r *Ring) []string {
	out := make([]string, r.BucketCount)
	for i := 0; i < r.BucketCount; i++ {
		out[i] = r.Owner(i)
	}
	return out
}

func sliceEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func abs(a int) int {
	if a < 0 {
		return -a
	}
	return a
}
