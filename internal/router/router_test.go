package router_test

import (
	"fmt"
	"sync"
	"testing"

	"flowrouter/internal/apperr"
	"flowrouter/internal/config"
	"flowrouter/internal/flow"
	"flowrouter/internal/router"
)

func members3() []config.Member {
	return []config.Member{
		{ID: "hop-a", Weight: 1},
		{ID: "hop-b", Weight: 1},
		{ID: "hop-c", Weight: 2},
	}
}

func newLoaded(t *testing.T) *router.Router {
	t.Helper()
	rt := router.New(160, 0)
	if _, changed, err := rt.Load(members3(), map[string]bool{}); err != nil || !changed {
		t.Fatalf("initial load: changed=%v err=%v", changed, err)
	}
	return rt
}

func sampleFlow() flow.FiveTuple {
	f, err := flow.Parse("10.0.0.1", "10.1.0.1", 6, 12345, 80)
	if err != nil {
		panic(err)
	}
	return f
}

func TestInitialLoadAndVersion(t *testing.T) {
	rt := newLoaded(t)
	s := rt.Current()
	if s.Version != 1 {
		t.Fatalf("version=%d want 1", s.Version)
	}
	if s.NumOnRing != 3 || s.UpTotalWeight != 4 {
		t.Fatalf("on_ring=%d up_weight=%d", s.NumOnRing, s.UpTotalWeight)
	}
	// double load is a state conflict, not a silent overwrite
	if _, _, err := rt.Load(members3(), map[string]bool{}); err == nil {
		t.Fatal("second Load must fail")
	} else if ae, ok := apperr.As(err); !ok || ae.Code != "ALREADY_LOADED" {
		t.Fatalf("err=%v", err)
	}
}

func TestStableRoutingSameGeneration(t *testing.T) {
	rt := newLoaded(t)
	f := sampleFlow()
	first, err := rt.Route(f)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 50; i++ {
		d, err := rt.Route(f)
		if err != nil {
			t.Fatal(err)
		}
		if d.MemberID != first.MemberID || d.Version != 1 {
			t.Fatalf("routing changed within generation: %s v%d vs %s v1",
				d.MemberID, d.Version, first.MemberID)
		}
	}
}

func TestVersionCASConflict(t *testing.T) {
	rt := newLoaded(t)
	// stale expected version must be rejected with STATE_CONFLICT/VERSION
	_, _, err := rt.SetDown(999, "hop-a", "test")
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindStateConflict || ae.Code != "VERSION" {
		t.Fatalf("err=%v, want STATE_CONFLICT/VERSION", err)
	}
	// successful transition bumps version
	s2, changed, err := rt.SetDown(1, "hop-a", "test")
	if err != nil || !changed || s2.Version != 2 {
		t.Fatalf("setdown: v=%d changed=%v err=%v", s2.Version, changed, err)
	}
	// idempotent no-op with correct version reports changed=false, no bump
	s2b, changed, err := rt.SetDown(2, "hop-a", "test")
	if err != nil || changed || s2b.Version != 2 {
		t.Fatalf("repeat down: changed=%v v=%d err=%v", changed, s2b.Version, err)
	}
}

func TestImmediateExclusionAndRecovery(t *testing.T) {
	rt := newLoaded(t)
	flows := buildFlows(400)

	// snapshot owners at v1
	ownerV1 := map[string]string{}
	for _, f := range flows {
		d, err := rt.Route(f)
		if err != nil {
			t.Fatal(err)
		}
		ownerV1[f.CanonicalKey()] = d.MemberID
	}

	// mark hop-a down: immediate, no flows can route to it
	s2, _, err := rt.SetDown(rt.Current().Version, "hop-a", "probe_failed")
	if err != nil {
		t.Fatal(err)
	}
	if s2.Version != 2 {
		t.Fatalf("v=%d", s2.Version)
	}
	for _, mi := range s2.Members {
		if mi.ID == "hop-a" && (mi.Up || mi.OnRing) {
			t.Fatal("hop-a must be up=false on_ring=false immediately")
		}
	}
	for _, f := range flows {
		d, err := rt.Route(f)
		if err != nil {
			t.Fatal(err)
		}
		if d.MemberID == "hop-a" {
			t.Fatalf("flow %s still routed to excluded hop-a", f.CanonicalKey())
		}
	}

	// recovery: versioned reassignment. Owners must return EXACTLY to v1 for
	// every flow because member inputs are identical again.
	s3, _, err := rt.SetUp(rt.Current().Version, "hop-a")
	if err != nil {
		t.Fatal(err)
	}
	if s3.Version != 3 {
		t.Fatalf("recovery version=%d want 3", s3.Version)
	}
	movedDuringRecovery := 0
	for _, f := range flows {
		d, err := rt.Route(f)
		if err != nil {
			t.Fatal(err)
		}
		if d.MemberID != ownerV1[f.CanonicalKey()] {
			movedDuringRecovery++
		}
	}
	// Exact-zero tolerance assertion: deterministic ring with the same inputs
	// restores every prior mapping after recovery.
	if movedDuringRecovery != 0 {
		t.Fatalf("%d flows differ after full recovery; expected 0 (rule: versioned deterministic reassignment)",
			movedDuringRecovery)
	}
}

func TestDownUnknownMember(t *testing.T) {
	rt := newLoaded(t)
	_, _, err := rt.SetDown(1, "ghost", "x")
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindInvalidInput || ae.Code != "UNKNOWN_MEMBER" {
		t.Fatalf("err=%v", err)
	}
}

func TestZeroWeightMemberNeverReceives(t *testing.T) {
	rt := router.New(64, 0)
	members := []config.Member{
		{ID: "a", Weight: 1}, {ID: "b", Weight: 1}, {ID: "z", Weight: 0},
	}
	snap, _, err := rt.Load(members, nil)
	if err != nil {
		t.Fatal(err)
	}
	if snap.NumOnRing != 2 {
		t.Fatalf("on_ring=%d want 2 (zero-weight member excluded)", snap.NumOnRing)
	}
	for _, f := range buildFlows(500) {
		d, err := rt.Route(f)
		if err != nil {
			t.Fatal(err)
		}
		if d.MemberID == "z" {
			t.Fatalf("zero-weight member received flow %s", f.CanonicalKey())
		}
	}
	// weight zero still tracks the member identity
	var found bool
	for _, mi := range snap.Members {
		if mi.ID == "z" {
			found = true
			if mi.OnRing || mi.VNodes != 0 {
				t.Fatal("zero-weight member on ring")
			}
		}
	}
	if !found {
		t.Fatal("zero-weight member missing from snapshot")
	}

	// giving it a positive weight admits it via a version bump
	s2, changed, err := rt.SetWeight(rt.Current().Version, "z", 1)
	if err != nil || !changed {
		t.Fatalf("setweight: changed=%v err=%v", changed, err)
	}
	if s2.NumOnRing != 3 {
		t.Fatalf("on_ring after promote=%d", s2.NumOnRing)
	}
}

func TestAllMembersDownNoHealthy(t *testing.T) {
	rt := newLoaded(t)
	v := rt.Current().Version
	for _, id := range []string{"hop-a", "hop-b", "hop-c"} {
		s, _, err := rt.SetDown(v, id, "test")
		if err != nil {
			t.Fatal(err)
		}
		v = s.Version
	}
	if rt.Current().NumUp != 0 || !rt.Current().Ring.Empty() {
		t.Fatal("expected empty ring with all members down")
	}
	_, err := rt.Route(sampleFlow())
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindNoHealthyMember || ae.Code != "ALL_DOWN" {
		t.Fatalf("err=%v, want NO_HEALTHY_MEMBER/ALL_DOWN", err)
	}

	// all zero weights is a distinct code
	rt2 := router.New(16, 0)
	_, _, _ = rt2.Load([]config.Member{{ID: "a", Weight: 0}, {ID: "b", Weight: 0}}, nil)
	_, err = rt2.Route(sampleFlow())
	ae, ok = apperr.As(err)
	if !ok || ae.Code != "ZERO_TOTAL_WEIGHT" {
		t.Fatalf("err=%v, want ZERO_TOTAL_WEIGHT", err)
	}

	// no members at all is a third distinct code
	rt3 := router.New(16, 0)
	_, _, _ = rt3.Load(nil, nil)
	_, err = rt3.Route(sampleFlow())
	ae, _ = apperr.As(err)
	if ae == nil || ae.Code != "NO_MEMBERS" {
		t.Fatalf("err=%v, want NO_MEMBERS", err)
	}
}

func TestConcurrentConfigReadsWhileMutating(t *testing.T) {
	rt := newLoaded(t)
	flows := buildFlows(200)

	var wg sync.WaitGroup
	stop := make(chan struct{})
	// readers: continuously route and read snapshots concurrently
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				s := rt.Current()
				if s == nil {
					t.Error("nil snapshot")
					return
				}
				for _, f := range flows {
					_, _ = rt.Route(f) // must not panic/race regardless of outcome
				}
			}
		}()
	}
	// writer: flap hop-a down/up many times with correct CAS versions
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 20; i++ {
			v := rt.Current().Version
			if _, _, err := rt.SetDown(v, "hop-a", "race"); err != nil {
				t.Errorf("setdown: %v", err)
				return
			}
			v = rt.Current().Version
			if _, _, err := rt.SetUp(v, "hop-a"); err != nil {
				t.Errorf("setup: %v", err)
				return
			}
		}
		close(stop)
	}()
	wg.Wait()

	// end state: hop-a back up and routing again
	if rt.Current().NumOnRing != 3 {
		t.Fatalf("final on_ring=%d", rt.Current().NumOnRing)
	}
}

func TestNegativeWeightRejected(t *testing.T) {
	rt := newLoaded(t)
	_, _, err := rt.SetWeight(1, "hop-a", -5)
	if ae, ok := apperr.As(err); !ok || ae.Code != "NEGATIVE_WEIGHT" {
		t.Fatalf("err=%v", err)
	}
}

func TestCountersAreNotShares(t *testing.T) {
	rt := newLoaded(t)
	for _, f := range buildFlows(300) {
		if _, err := rt.Route(f); err != nil {
			t.Fatal(err)
		}
	}
	c := rt.Counts()
	var sum int64
	for _, n := range c.PerMember {
		sum += n
	}
	if sum != c.TotalRouted {
		t.Fatalf("per-member sum %d != total routed %d", sum, c.TotalRouted)
	}
	if c.TotalRouted != 300 {
		t.Fatalf("total routed %d", c.TotalRouted)
	}
}

func buildFlows(n int) []flow.FiveTuple {
	out := make([]flow.FiveTuple, 0, n)
	for i := 0; i < n; i++ {
		f, err := flow.Parse(
			fmt.Sprintf("10.0.0.%d", 1+(i%50)),
			fmt.Sprintf("10.1.0.%d", 1+((i/50)%50)),
			[]uint8{6, 17}[i%2],
			uint16(1000+i),
			uint16(80+(i%4)),
		)
		if err != nil {
			panic(err)
		}
		out = append(out, f)
	}
	return out
}
