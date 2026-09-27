package engine

import (
	"testing"

	"pathvector/internal/config"
	"pathvector/internal/model"
)

func inlineIdx(t *testing.T, topo model.Topology) *model.Index {
	t.Helper()
	idx, err := topo.Build()
	if err != nil {
		t.Fatal(err)
	}
	return idx
}

// TestExportPrependInASPath: an export prepend of 2 must make the eBGP
// outbound path [ownAS ownAS ownAS origin...] (mandatory prepend + 2 extra).
func TestExportPrependInASPath(t *testing.T) {
	topo := model.Topology{Nodes: []model.Node{
		{RouterID: "src", ASN: 65010, Address: "10.0.0.1", IGPCost: 1},
		{RouterID: "mid", ASN: 65020, Address: "10.0.0.2", IGPCost: 1},
		{RouterID: "dst", ASN: 65030, Address: "10.0.0.3", IGPCost: 1},
	}, Links: []model.Link{{Src: "src", Dst: "mid"}, {Src: "mid", Dst: "dst"}}}
	idx := inlineIdx(t, topo)
	lp := uint32(300)
	policies := map[string]*config.Policy{
		"mid": {Export: []config.Rule{{
			Name: "inflate-to-dst", Match: config.Match{Peer: "dst"},
			Action: config.Action{Allow: true, Prepend: 2},
		}}},
	}
	events := []model.SeedEvent{
		{Seq: 1, Kind: model.SeedAnnounce, RouterID: "src", Prefix: "203.0.113.0/24", NextHop: "10.0.0.1", Origin: "igp"},
	}
	rep, err := Run("run-prepend", idx, policies, events, Options{Budget: 50, QueueCap: 100})
	if err != nil {
		t.Fatal(err)
	}
	c, ok := rep.BestRoutes["203.0.113.0/24"]["dst"]
	if !ok {
		t.Fatal("dst has no route")
	}
	want := []int{65020, 65020, 65020, 65010}
	if !eqInts(c.ASPath, want) {
		t.Fatalf("dst path=%v want %v (mandatory + 2 prepends)", c.ASPath, want)
	}
	_ = lp
}

// TestIBGPSplitHorizon: an iBGP-learned route must not be relayed to
// another router in the same AS, but it MAY leave the AS over eBGP.
func TestIBGPSplitHorizon(t *testing.T) {
	topo := model.Topology{Nodes: []model.Node{
		{RouterID: "ext", ASN: 65099, Address: "10.0.0.9", IGPCost: 1},
		{RouterID: "br1", ASN: 65001, Address: "10.1.0.1", IGPCost: 1},
		{RouterID: "br2", ASN: 65001, Address: "10.1.0.2", IGPCost: 1},
		{RouterID: "rr", ASN: 65001, Address: "10.1.0.3", IGPCost: 1},
		// Downstream eBGP customer of rr: allowed destination for an
		// iBGP-learned route.
		{RouterID: "cust", ASN: 65077, Address: "10.2.0.1", IGPCost: 1},
	}, Links: []model.Link{
		{Src: "ext", Dst: "br1"},
		{Src: "br1", Dst: "rr"},
		{Src: "rr", Dst: "br2"},
		{Src: "rr", Dst: "cust"},
	}}
	idx := inlineIdx(t, topo)
	events := []model.SeedEvent{
		{Seq: 1, Kind: model.SeedAnnounce, RouterID: "ext", Prefix: "198.51.100.0/24", NextHop: "10.0.0.9", Origin: "igp"},
	}
	rep, err := Run("run-ibgp-sh", idx, nil, events, Options{Budget: 100, QueueCap: 100})
	if err != nil {
		t.Fatal(err)
	}
	const pfx = "198.51.100.0/24"
	// rr learns over iBGP from br1.
	if c, ok := rep.BestRoutes[pfx]["rr"]; !ok || c.FromPeer != "br1" || !c.LearnedIBGP {
		t.Fatalf("rr route=%+v ok=%v, want iBGP from br1", c, ok)
	}
	// ...and must NOT relay it iBGP to br2.
	if _, ok := rep.BestRoutes[pfx]["br2"]; ok {
		t.Fatal("br2 must not receive an iBGP-learned route relayed by rr")
	}
	// The eBGP customer, however, must receive it with 65001 prepended.
	cust, ok := rep.BestRoutes[pfx]["cust"]
	if !ok {
		t.Fatal("cust must receive the route over eBGP from rr")
	}
	if cust.LearnedIBGP || cust.ASPath[0] != 65001 {
		t.Fatalf("cust route ibgp=%v path=%v, want eBGP path starting 65001", cust.LearnedIBGP, cust.ASPath)
	}
	for _, ev := range rep.Trace {
		if ev.Router != "rr" {
			continue
		}
		for _, q := range ev.Queued {
			if q.To == "br2" {
				t.Fatalf("rr queued an UPDATE to iBGP peer br2: %+v", q)
			}
			if q.To == "cust" && q.Kind == "announce" {
				if len(q.ASPath) == 0 || q.ASPath[0] != 65001 {
					t.Fatalf("rr -> cust path=%v must start with 65001", q.ASPath)
				}
			}
		}
	}
}

// TestLocalOriginWithdraw: a withdraw with no peer removes only the local
// origin; a route learned from an upstream neighbor survives and is promoted.
func TestLocalOriginWithdraw(t *testing.T) {
	// p learns the prefix from upstream u (so its candidate carries
	// [65030], not p's own ASN), then relays it eBGP to r.
	topo := model.Topology{Nodes: []model.Node{
		{RouterID: "u", ASN: 65030, Address: "10.0.0.3", IGPCost: 1},
		{RouterID: "r", ASN: 65001, Address: "10.0.0.1", IGPCost: 1},
		{RouterID: "p", ASN: 65020, Address: "10.0.0.2", IGPCost: 1},
	}, Links: []model.Link{{Src: "u", Dst: "p"}, {Src: "r", Dst: "p"}}}
	idx := inlineIdx(t, topo)
	events := []model.SeedEvent{
		{Seq: 1, Kind: model.SeedAnnounce, RouterID: "u", Prefix: "10.0.0.0/8", NextHop: "10.0.0.3", Origin: "igp"},
		{Seq: 2, Kind: model.SeedAnnounce, RouterID: "r", Prefix: "10.0.0.0/8", NextHop: "10.0.0.1", Origin: "igp"},
		{Seq: 3, Kind: model.SeedWithdraw, RouterID: "r", Prefix: "10.0.0.0/8"},
	}
	rep, err := Run("run-localwd", idx, nil, events, Options{Budget: 50, QueueCap: 100})
	if err != nil {
		t.Fatal(err)
	}
	// While r's local origin exists it wins (source rank); after the
	// withdraw, p's relayed candidate must be promoted.
	c, ok := rep.BestRoutes["10.0.0.0/8"]["r"]
	if !ok || c.FromPeer != "p" {
		t.Fatalf("r after local withdraw = %+v ok=%v, want p promoted", c, ok)
	}
	if !eqInts(c.ASPath, []int{65020, 65030}) {
		t.Fatalf("promoted path=%v want [65020 65030]", c.ASPath)
	}
}
