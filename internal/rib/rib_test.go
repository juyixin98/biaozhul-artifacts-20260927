package rib

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/opp221/ribd/internal/netmodel"
)

func rt(id, pfx string, ad int, metric uint32, nh netmodel.NextHop) netmodel.Route {
	return netmodel.Route{ID: id, Prefix: netmodel.MustPrefix(pfx), AdminDist: ad, Metric: metric, NextHop: nh}
}

func via(s string) netmodel.NextHop {
	a, err := netmodel.ParseAddr(s)
	if err != nil {
		panic(err)
	}
	return netmodel.NextHop{Addr: a}
}

func dev(s string) netmodel.NextHop { return netmodel.NextHop{Interface: s} }

// TestBatchIsAtomicOrNothing: a batch whose second change is invalid must not
// install the first one, and the version must not move.
func TestBatchIsAtomicOrNothing(t *testing.T) {
	tbl := NewTable(3, nil)
	bad := []Change{
		{Kind: Upsert, Route: rt("ok", "10.0.0.0/8", 10, 0, dev("eth0"))},
		{Kind: Upsert, Route: rt("", "10.0.0.0/8", 10, 0, dev("eth0"))},
	}
	snap, err := tbl.Apply(bad)
	if err == nil {
		t.Fatal("invalid batch accepted")
	}
	if got := snap.VersionNum(); got != 0 {
		t.Fatalf("version moved to %d after rejected batch", got)
	}
	if _, ok := snap.RouteByID("ok"); ok {
		t.Fatal("first change of a rejected batch became visible")
	}
	if n := len(snap.Routes()); n != 0 {
		t.Fatalf("routes visible after rejected batch: %d", n)
	}
}

// TestBatchWholeSetVisibleAtOneVersion verifies the reader-facing guarantee:
// all routes of one batch resolve against the same new version.
func TestBatchWholeSetVisibleAtOneVersion(t *testing.T) {
	tbl := NewTable(3, nil)
	batch := []Change{
		{Kind: Upsert, Route: rt("d", "0.0.0.0/0", 10, 0, dev("eth0"))},
		{Kind: Upsert, Route: rt("p", "203.0.113.0/24", 0, 0, via("203.0.113.10"))},
	}
	// Note p2p's next hop is within the same batch's connected segment only
	// if connected is present; use a direct route instead:
	batch[1] = Change{Kind: Upsert, Route: rt("p", "203.0.113.0/24", 0, 0, dev("eth1"))}
	snap, err := tbl.Apply(batch)
	if err != nil {
		t.Fatal(err)
	}
	if snap.VersionNum() != 1 {
		t.Fatalf("version = %d, want 1", snap.VersionNum())
	}
	for _, r := range snap.Routes() {
		if r.InstalledVersion != 1 {
			t.Errorf("route %s installed_version=%d, want 1", r.ID, r.InstalledVersion)
		}
	}
	res := snap.LookupText("198.51.100.1")
	if res.TableVersion != 1 || res.ChosenRoute != "d" {
		t.Fatalf("lookup = v%d route %s, want v1 d", res.TableVersion, res.ChosenRoute)
	}
}

// TestOldSnapshotStillReadableAfterNextBatch is the persistence/visibility
// claim: a snapshot held by a reader is never mutated by later batches.
func TestOldSnapshotStillReadableAfterNextBatch(t *testing.T) {
	tbl := NewTable(3, nil)
	s1, err := tbl.Apply([]Change{{Kind: Upsert, Route: rt("a", "10.0.0.0/8", 10, 0, dev("eth0"))}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tbl.Apply([]Change{{Kind: Upsert, Route: rt("b", "10.1.0.0/16", 5, 0, dev("eth1"))}}); err != nil {
		t.Fatal(err)
	}
	// s1 must still show exactly one route and resolve 10.2.0.0 via a.
	if n := len(s1.Routes()); n != 1 {
		t.Fatalf("old snapshot mutated: %d routes", n)
	}
	res := s1.LookupText("10.2.0.0")
	if res.ChosenRoute != "a" {
		t.Fatalf("old snapshot chose %s, want a", res.ChosenRoute)
	}
}

// TestFamiliesNeverCross: installing an IPv6 default must not answer IPv4
// queries and vice versa.
func TestFamiliesNeverCross(t *testing.T) {
	tbl := NewTable(3, nil)
	if _, err := tbl.Apply([]Change{{Kind: Upsert, Route: rt("v6d", "::/0", 200, 0, dev("eth0"))}}); err != nil {
		t.Fatal(err)
	}
	res := tbl.Current().LookupText("192.0.2.1")
	if res.Status != StatusIndeterminate || res.Failure != FailureNoRoute {
		t.Fatalf("v4 query against v6-only table: %s/%s", res.Status, res.Failure)
	}
	ok := tbl.Current().LookupText("2001:db8::1")
	if ok.Status != StatusResolved || ok.Egress != "eth0" {
		t.Fatalf("v6 query: %s/%s", ok.Status, ok.Failure)
	}
}

// TestLongestPrefixBeatsAdminDistance is the core selection rule.
func TestLongestPrefixBeatsAdminDistance(t *testing.T) {
	tbl := NewTable(3, nil)
	_, _ = tbl.Apply([]Change{
		{Kind: Upsert, Route: rt("def", "0.0.0.0/0", 1, 0, dev("eth0"))},      // preferred AD
		{Kind: Upsert, Route: rt("net", "10.10.0.0/16", 200, 0, dev("eth1"))}, // worse AD, longer
	})
	res := tbl.Current().LookupText("10.10.1.1")
	if res.ChosenRoute != "net" {
		t.Fatalf("longer prefix must win regardless of AD; chose %s", res.ChosenRoute)
	}
}

// TestSamePrefixPolicyADMetricSequence asserts the fixed tie-break at one
// prefix: AD, then metric, then install sequence; re-announce keeps seniority.
func TestSamePrefixPolicyADMetricSequence(t *testing.T) {
	tbl := NewTable(3, nil)
	// AD comparison
	_, _ = tbl.Apply([]Change{
		{Kind: Upsert, Route: rt("ad200", "10.0.0.0/8", 200, 0, dev("a"))},
		{Kind: Upsert, Route: rt("ad50", "10.0.0.0/8", 50, 0, dev("b"))},
	})
	if best := bestIDAt(t, tbl.Current(), "10.0.0.0/8"); best != "ad50" {
		t.Fatalf("AD policy chose %s", best)
	}

	// Metric comparison with equal AD, insertion order deliberately reversed
	tbl2 := NewTable(3, nil)
	_, _ = tbl2.Apply([]Change{
		{Kind: Upsert, Route: rt("m-high", "10.0.0.0/8", 50, 100, dev("a"))},
		{Kind: Upsert, Route: rt("m-low", "10.0.0.0/8", 50, 1, dev("b"))},
	})
	if best := bestIDAt(t, tbl2.Current(), "10.0.0.0/8"); best != "m-low" {
		t.Fatalf("metric policy chose %s", best)
	}

	// Sequence: first installed wins the final tie
	tbl3 := NewTable(3, nil)
	first := rt("first", "10.0.0.0/8", 50, 1, dev("a"))
	_, _ = tbl3.Apply([]Change{{Kind: Upsert, Route: first}})
	_, _ = tbl3.Apply([]Change{{Kind: Upsert, Route: rt("second", "10.0.0.0/8", 50, 1, dev("b"))}})
	if best := bestIDAt(t, tbl3.Current(), "10.0.0.0/8"); best != "first" {
		t.Fatalf("sequence tie-break chose %s", best)
	}
	// Re-announce first with changed metric must keep its sequence but win
	// because its metric is now better? Same metric here; it should stay first.
	r, _ := tbl3.Current().RouteByID("first")
	r.Metric = 1
	if _, err := tbl3.Apply([]Change{{Kind: Upsert, Route: r}}); err != nil {
		t.Fatal(err)
	}
	if best := bestIDAt(t, tbl3.Current(), "10.0.0.0/8"); best != "first" {
		t.Fatalf("re-announce changed seniority: chose %s", best)
	}
}

func bestIDAt(t *testing.T, s *Snapshot, pfx string) string {
	t.Helper()
	p := netmodel.MustPrefix(pfx)
	chain := s.match(p.Addr())
	for _, c := range chain {
		if c.Prefix == pfx {
			return c.Best.ID
		}
	}
	t.Fatalf("no set at %s", pfx)
	return ""
}

// TestDeleteSemantics: deleting an unknown id rejects the batch; delete then
// read shows fallthrough.
func TestDeleteSemantics(t *testing.T) {
	tbl := NewTable(3, nil)
	_, _ = tbl.Apply([]Change{{Kind: Upsert, Route: rt("a", "10.0.0.0/8", 10, 0, dev("eth0"))}})
	if _, err := tbl.Apply([]Change{{Kind: Delete, Route: netmodel.Route{ID: "ghost"}}}); err == nil {
		t.Fatal("delete of unknown id accepted")
	}
	s2, err := tbl.Apply([]Change{{Kind: Delete, Route: netmodel.Route{ID: "a"}}})
	if err != nil {
		t.Fatal(err)
	}
	if n := len(s2.Routes()); n != 0 {
		t.Fatalf("route survived delete: %d", n)
	}
}

// TestChangeJSONShape enforces that the wire format rejects the legacy
// top-level "id" and delete bodies carrying route fields.
func TestChangeJSONShape(t *testing.T) {
	var c Change
	if err := json.Unmarshal([]byte(`{"kind":"delete","route":{"id":"x","prefix":"10.0.0.0/8"}}`), &c); err == nil {
		t.Fatal("delete with prefix accepted")
	}
	var c2 Change
	if err := json.Unmarshal([]byte(`{"kind":"delete","id":"x","route":{"id":"x"}}`), &c2); err == nil {
		t.Fatal("top-level id accepted")
	}
	var c3 Change
	if err := json.Unmarshal([]byte(`{"kind":"frob","route":{"id":"x"}}`), &c3); err == nil {
		t.Fatal("unknown kind accepted")
	}
	var c4 Change
	if err := json.Unmarshal([]byte(`{"kind":"upsert","route":{"id":"x","prefix":"10.0.0.0/8","admin_distance":1,"next_hop":{"interface":"eth0"}}}`), &c4); err != nil {
		t.Fatalf("valid upsert rejected: %v", err)
	}
}

// TestChangingPrefixOfRouteID moves the route rather than duplicating it.
func TestChangingPrefixOfRouteID(t *testing.T) {
	tbl := NewTable(3, nil)
	_, _ = tbl.Apply([]Change{{Kind: Upsert, Route: rt("a", "10.0.0.0/8", 10, 0, dev("eth0"))}})
	_, err := tbl.Apply([]Change{{Kind: Upsert, Route: rt("a", "10.2.0.0/16", 10, 0, dev("eth1"))}})
	if err != nil {
		t.Fatal(err)
	}
	s := tbl.Current()
	if n := len(s.Routes()); n != 1 {
		t.Fatalf("re-announce at new prefix left %d routes", n)
	}
	res := s.LookupText("10.1.1.1")
	if res.Status != StatusIndeterminate {
		t.Fatalf("old prefix still routed: %s", res.Status)
	}
	if res := s.LookupText("10.2.1.1"); res.Egress != "eth1" {
		t.Fatalf("new prefix not routed: %s", res.Egress)
	}
}

func TestReasonTextNotEmptyAndNamesCategory(t *testing.T) {
	tbl := NewTable(2, nil)
	_, _ = tbl.Apply([]Change{
		{Kind: Upsert, Route: rt("a", "10.0.0.1/32", 5, 0, via("10.0.0.2"))},
		{Kind: Upsert, Route: rt("b", "10.0.0.2/32", 5, 0, via("10.0.0.3"))},
		{Kind: Upsert, Route: rt("c", "10.0.0.3/32", 5, 0, dev("eth9"))},
	})
	res := tbl.Current().LookupText("10.0.0.1")
	if res.Status != StatusResolved {
		t.Fatalf("depth=1 chain a->b->c: %s %s", res.Status, res.Reason)
	}
	if !strings.Contains(res.Reason, "table v") {
		t.Fatalf("reason should name table version: %q", res.Reason)
	}
}
