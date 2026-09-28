package replay

import (
	"context"
	"net/netip"
	"testing"

	"rib/internal/netmodel"
	"rib/internal/rib"
	"rib/internal/store"
)

func ptr(a netip.Addr) *netip.Addr { return &a }

func setup(t *testing.T) (*store.Store, *rib.RIB) {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, "file:"+t.Name()+"?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })

	r := rib.New()
	rt := func(id, pfx, nh string, ad int) netmodel.Route {
		a, _ := netip.ParseAddr(nh)
		return netmodel.Route{
			ID: id, Prefix: netmodel.MustPrefix(pfx), AdminDistance: ad,
			Protocol: "static",
			Nexthop:  netmodel.Nexthop{Kind: netmodel.NHAddress, Address: ptr(a.Unmap())},
		}
	}
	// 通过“先改 RIB、再提交事件”的方式制造日志，模拟真实处理器路径。
	apply := func(rt netmodel.Route, ver int64) {
		if err := r.Upsert(rt); err != nil {
			t.Fatal(err)
		}
		if _, err := st.CommitUpsert(ctx, rt, ver, "test"); err != nil {
			t.Fatal(err)
		}
	}
	apply(rt("a", "10.0.0.0/8", "10.0.0.1", 5), r.Version())
	apply(rt("b", "192.168.0.0/16", "192.168.0.1", 5), r.Version())
	// 删除 a。
	if err := r.Delete(netmodel.MustPrefix("10.0.0.0/8"), "a"); err != nil {
		t.Fatal(err)
	}
	if _, err := st.CommitDelete(ctx, int(netmodel.AFIPv4), "10.0.0.0/8", "a", r.Version(), "test"); err != nil {
		t.Fatal(err)
	}
	return st, r
}

func TestReplayRebuildsCurrentState(t *testing.T) {
	st, r := setup(t)
	ctx := context.Background()
	current := append(r.Routes(netmodel.AFIPv4), r.Routes(netmodel.AFIPv6)...)

	rep, err := New(st).Run(ctx, 0, 0, current, 16)
	if err != nil {
		t.Fatal(err)
	}
	if rep.EventsPlayed != 3 {
		t.Fatalf("events=%d want 3", rep.EventsPlayed)
	}
	if !rep.Consistent {
		t.Fatalf("replay inconsistent: %v", rep.Mismatches)
	}
	if rep.ReplayedV4 != 1 || rep.CurrentV4 != 1 {
		t.Fatalf("counts replay=%d/%d current=%d/%d", rep.ReplayedV4, rep.ReplayedV6, rep.CurrentV4, rep.CurrentV6)
	}
	// 删除事件必须在回放中真实生效（a 被移除，b 保留）。
	for _, h := range rep.Hops {
		if !h.Applied {
			t.Fatalf("hop not applied: %+v", h)
		}
	}
}

func TestReplayDetectsDivergence(t *testing.T) {
	st, _ := setup(t)
	ctx := context.Background()
	// 传入一个与日志重建结果不同的“当前表”，必须报不一致而非假装一致。
	phantom := netmodel.Route{
		ID: "phantom", Prefix: netmodel.MustPrefix("172.16.0.0/12"),
		AdminDistance: 1, Protocol: "static",
		Nexthop: netmodel.Nexthop{Kind: netmodel.NHBlackhole},
	}
	rep, err := New(st).Run(ctx, 0, 0, []netmodel.Route{phantom}, 16)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Consistent || len(rep.Mismatches) == 0 {
		t.Fatal("expected divergence to be reported")
	}
}

func TestReplaySinceSeq(t *testing.T) {
	st, r := setup(t)
	ctx := context.Background()
	// 只重放 seq>2（即最后一条 delete），重建结果与完整当前表必然不同：
	// a 会“复活”，b 不在日志窗口内。
	rep, err := New(st).Run(ctx, 2, 0,
		append(r.Routes(netmodel.AFIPv4), r.Routes(netmodel.AFIPv6)...), 16)
	if err != nil {
		t.Fatal(err)
	}
	if rep.EventsPlayed != 1 {
		t.Fatalf("played=%d want 1", rep.EventsPlayed)
	}
	if rep.Consistent {
		t.Fatal("partial replay must not claim consistency with full current state")
	}
}
