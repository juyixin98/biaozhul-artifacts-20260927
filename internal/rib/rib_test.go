package rib

import (
	"errors"
	"fmt"
	"net/netip"
	"sync"
	"testing"

	"rib/internal/netmodel"
)

func mustAddr(t *testing.T, s string) netip.Addr {
	t.Helper()
	a, err := netip.ParseAddr(s)
	if err != nil {
		t.Fatal(err)
	}
	return a.Unmap()
}

func ptr(a netip.Addr) *netip.Addr { return &a }

// rtBuilder 用紧凑参数构造路由，减少夹具噪音。
type rtBuilder struct {
	ad       int
	metric   int
	protocol string
}

func newRT(id, prefix, kind, nh string, ad, metric int, protocol string) netmodel.Route {
	r := netmodel.Route{
		ID:            id,
		Prefix:        netmodel.MustPrefix(prefix),
		AdminDistance: ad,
		Metric:        metric,
		Protocol:      protocol,
	}
	switch netmodel.NHKind(kind) {
	case netmodel.NHAddress:
		a, _ := netip.ParseAddr(nh)
		a = a.Unmap()
		r.Nexthop = netmodel.Nexthop{Kind: netmodel.NHAddress, Address: &a}
	case netmodel.NHConnected:
		r.Nexthop = netmodel.Nexthop{Kind: netmodel.NHConnected, Iface: nh}
	case netmodel.NHBlackhole:
		r.Nexthop = netmodel.Nexthop{Kind: netmodel.NHBlackhole}
	case netmodel.NHUnreachable:
		r.Nexthop = netmodel.Nexthop{Kind: netmodel.NHUnreachable}
	}
	return r
}

func mustUpsert(t *testing.T, r *RIB, routes ...netmodel.Route) {
	t.Helper()
	for _, rt := range routes {
		if err := r.Upsert(rt); err != nil {
			t.Fatalf("upsert %s: %v", rt.ID, err)
		}
	}
}

func chainIDs(res LookupResult) []string {
	out := make([]string, 0, len(res.Chain))
	for _, h := range res.Chain {
		out = append(out, h.Chosen.ID)
	}
	return out
}

func chainPrefixes(res LookupResult) []string {
	out := make([]string, 0, len(res.Chain))
	for _, h := range res.Chain {
		out = append(out, h.Prefix)
	}
	return out
}

// TestLPMOverAD 是关键约束：更长前缀必须胜出，即使它管理距离更差。
func TestLPMBeatsAdminDistance(t *testing.T) {
	r := New()
	// 默认路由 AD=1（很优），主机路由 AD=200（很差），主机路由仍须胜出。
	mustUpsert(t, r,
		newRT("def", "0.0.0.0/0", "address", "203.0.113.1", 1, 0, "static"),
		newRT("edge", "203.0.113.0/24", "connected", "eth0", 0, 0, "connected"),
		newRT("host", "10.10.10.1/32", "address", "203.0.113.254", 200, 0, "static"),
		newRT("net16", "10.10.0.0/16", "address", "203.0.113.2", 5, 0, "ospf"),
	)

	res := r.Lookup(mustAddr(t, "10.10.10.1"))
	if res.Status != StatusForwarded {
		t.Fatalf("status=%s want forwarded; diag=%v", res.Status, res.Diag)
	}
	wantIDs := []string{"host", "edge"}
	if got := chainIDs(res); fmt.Sprint(got) != fmt.Sprint(wantIDs) {
		t.Fatalf("chain=%v want %v", got, wantIDs)
	}
	if res.Egress != "eth0" {
		t.Fatalf("egress=%q want eth0", res.Egress)
	}

	// 落在 /16 但非主机：命中 net16 -> 203.0.113.2 -> edge。
	res = r.Lookup(mustAddr(t, "10.10.20.20"))
	wantIDs = []string{"net16", "edge"}
	if got := chainIDs(res); fmt.Sprint(got) != fmt.Sprint(wantIDs) {
		t.Fatalf("chain=%v want %v prefixes=%v", got, wantIDs, chainPrefixes(res))
	}
}

func TestDefaultRouteFallback(t *testing.T) {
	r := New()
	mustUpsert(t, r,
		newRT("def", "0.0.0.0/0", "connected", "dialer0", 255, 0, "static"),
	)
	res := r.Lookup(mustAddr(t, "198.51.100.77"))
	if res.Status != StatusForwarded || res.Egress != "dialer0" {
		t.Fatalf("got status=%s egress=%q", res.Status, res.Egress)
	}
	if len(res.Chain) != 1 || res.Chain[0].Prefix != "0.0.0.0/0" {
		t.Fatalf("default not sole hop: %+v", res.Chain)
	}
}

// TestSamePrefixPolicy 同前缀候选按固定策略：AD < metric < 协议 < 地址 < ID。
func TestSamePrefixPolicy(t *testing.T) {
	r := New()
	mustUpsert(t, r,
		newRT("a-low-metric", "10.0.0.0/8", "connected", "eth1", 5, 5, "static"),
		newRT("b-high-ad", "10.0.0.0/8", "connected", "eth2", 10, 0, "static"),
		newRT("c-high-metric", "10.0.0.0/8", "connected", "eth3", 5, 50, "static"),
	)
	res := r.Lookup(mustAddr(t, "10.1.1.1"))
	if res.Status != StatusForwarded || res.Egress != "eth1" {
		t.Fatalf("policy picked egress=%q status=%s; reason=%q",
			res.Egress, res.Status, res.Chain[0].Reason)
	}
	// 关键：AD 差距压过 metric（b 的 metric=0 但 AD=10）。
	if res.Chain[0].Chosen.ID != "a-low-metric" {
		t.Fatalf("chosen=%q", res.Chain[0].Chosen.ID)
	}

	// AD、metric 全平：协议固定先后 static(1) 优于 bgp(4)。
	r2 := New()
	mustUpsert(t, r2,
		newRT("bgp-route", "172.16.0.0/12", "connected", "bgp0", 5, 5, "bgp"),
		newRT("static-route", "172.16.0.0/12", "connected", "st0", 5, 5, "static"),
	)
	res2 := r2.Lookup(mustAddr(t, "172.16.5.5"))
	if res2.Chain[0].Chosen.ID != "static-route" {
		t.Fatalf("protocol tiebreak chose %q", res2.Chain[0].Chosen.ID)
	}

	// 全平（同 AD/metric/protocol/类型）：下一跳地址小者胜；再平则 ID 小者胜。
	r3 := New()
	mustUpsert(t, r3,
		newRT("tie-high", "192.0.2.0/24", "address", "192.0.2.200", 5, 5, "static"),
		newRT("tie-low", "192.0.2.0/24", "address", "192.0.2.1", 5, 5, "static"),
	)
	res3 := r3.Lookup(mustAddr(t, "192.0.2.99"))
	if res3.Chain[0].Chosen.ID != "tie-low" {
		t.Fatalf("address tiebreak chose %q", res3.Chain[0].Chosen.ID)
	}
}

func TestIPv6CompressedLookup(t *testing.T) {
	r := New()
	mustUpsert(t, r,
		newRT("v6def", "::/0", "connected", "ip6tun", 255, 0, "static"),
		newRT("v6edge", "2001:db8:1::/64", "connected", "eth1", 0, 0, "connected"),
		newRT("v6host", "2001:db8:2::1/128", "blackhole", "", 5, 0, "static"),
		newRT("v6doc", "2001:db8::/32", "address", "2001:db8:1::1", 5, 0, "static"),
	)

	// 链中前缀必须以压缩规范化形式出现。
	res := r.Lookup(mustAddr(t, "2001:db8:9::abcd"))
	if res.Status != StatusForwarded || res.Egress != "eth1" {
		t.Fatalf("status=%s egress=%q", res.Status, res.Egress)
	}
	if got := chainPrefixes(res); fmt.Sprint(got) != fmt.Sprint([]string{"2001:db8::/32", "2001:db8:1::/64"}) {
		t.Fatalf("v6 chain prefixes=%v", got)
	}

	// 大写/非压缩写法目标必须等价命中。
	res = r.Lookup(mustAddr(t, "2001:DB8:0009:0000:0000:0000:0000:ABCD"))
	if res.Status != StatusForwarded {
		t.Fatalf("noncanonical target status=%s", res.Status)
	}

	// 主机黑洞。
	res = r.Lookup(mustAddr(t, "2001:db8:2::1"))
	if res.Status != StatusBlackhole || len(res.Chain) != 1 || res.Chain[0].Prefix != "2001:db8:2::1/128" {
		t.Fatalf("blackhole lookup wrong: status=%s chain=%v", res.Status, chainPrefixes(res))
	}

	// v6 默认兜底。
	res = r.Lookup(mustAddr(t, "2606:4700::1"))
	if res.Status != StatusForwarded || res.Egress != "ip6tun" || res.Chain[0].Prefix != "::/0" {
		t.Fatalf("v6 default fallback failed: %+v", res)
	}
}

// TestAFIsolation：IPv4 查询绝不命中 IPv6 树，反之亦然。
func TestAFIsolation(t *testing.T) {
	r := New()
	mustUpsert(t, r,
		newRT("only-v4-default", "0.0.0.0/0", "blackhole", "", 1, 0, "static"),
		newRT("only-v6-edge", "2001:db8:1::/64", "connected", "eth1", 0, 0, "connected"),
	)
	// IPv6 目标没有 ::/0，必须 no_route，不得借道 v4 默认。
	res := r.Lookup(mustAddr(t, "2001:dead::1"))
	if res.Status != StatusNoRoute {
		t.Fatalf("AF leak: v6 target got %s (%v)", res.Status, chainIDs(res))
	}
	// v4 目标命中黑洞，与 v6 表无关。
	res = r.Lookup(mustAddr(t, "8.8.8.8"))
	if res.Status != StatusBlackhole {
		t.Fatalf("v4 target status=%s", res.Status)
	}
}

// TestRecursionLoop 3 跳不同前缀的环必须被检出并归类。
func TestRecursionLoop(t *testing.T) {
	r := New()
	mustUpsert(t, r,
		newRT("p1", "10.0.0.0/8", "address", "10.1.0.1", 5, 0, "static"),
		newRT("p2", "10.1.0.0/16", "address", "10.1.2.1", 5, 0, "static"),
		newRT("p3", "10.1.2.0/24", "address", "10.0.0.1", 5, 0, "static"),
	)
	res := r.Lookup(mustAddr(t, "10.1.2.3"))
	if res.Status != StatusRecursionLoop {
		t.Fatalf("status=%s want recursion_loop; chain=%v", res.Status, chainIDs(res))
	}
	// LPM 起点必为最具体的 p3；随后 p3 的下一跳落在 /8，依次 p1->p2->p3 重入。
	wantIDs := []string{"p3", "p1", "p2", "p3"}
	if got := chainIDs(res); fmt.Sprint(got) != fmt.Sprint(wantIDs) {
		t.Fatalf("loop chain=%v want %v", got, wantIDs)
	}
	if len(res.Diag) == 0 {
		t.Fatal("loop result must carry diagnostics explaining rejection")
	}

	// 自环（下一跳落在自身前缀）：同一 /32 指向自身地址。
	r2 := New()
	mustUpsert(t, r2,
		newRT("self", "192.0.2.1/32", "address", "192.0.2.1", 5, 0, "static"),
	)
	res2 := r2.Lookup(mustAddr(t, "192.0.2.1"))
	if res2.Status != StatusRecursionLoop {
		t.Fatalf("self-loop status=%s chain=%v", res2.Status, chainIDs(res2))
	}
}

// TestUnresolvedNexthop：递归下一跳没有任何覆盖前缀。
func TestUnresolvedNexthop(t *testing.T) {
	r := New()
	mustUpsert(t, r,
		newRT("net", "10.0.0.0/8", "address", "172.16.0.1", 5, 0, "static"),
	)
	res := r.Lookup(mustAddr(t, "10.9.9.9"))
	if res.Status != StatusNexthopUnresolved {
		t.Fatalf("status=%s want nexthop_unresolved", res.Status)
	}
	if res.Depth != 1 {
		t.Fatalf("depth=%d want 1", res.Depth)
	}
}

func TestNoRoute(t *testing.T) {
	r := New()
	res := r.Lookup(mustAddr(t, "203.0.113.1"))
	if res.Status != StatusNoRoute || len(res.Chain) != 0 {
		t.Fatalf("empty table lookup: %+v", res)
	}
}

func TestUnreachableTerminates(t *testing.T) {
	r := New()
	mustUpsert(t, r, newRT("u", "100.64.0.0/10", "unreachable", "", 5, 0, "static"))
	res := r.Lookup(mustAddr(t, "100.64.0.1"))
	if res.Status != StatusUnreachable || res.Depth != 1 {
		t.Fatalf("status=%s depth=%d", res.Status, res.Depth)
	}
}

// TestDepthLimit：长度超过上限的无环节点链。
func TestDepthLimit(t *testing.T) {
	// 构造 10.0.0.1 -> 10.128.0.1 -> 10.192.0.1 -> 10.224.0.1 ...
	// 用逐段加长前缀形成单调链，无环，深度上限 3 时必须 depth_exceeded。
	r := New().WithMaxDepth(3)
	hops := []struct{ pfx, nh string }{
		{"10.0.0.0/8", "10.128.0.1"},
		{"10.128.0.0/9", "10.192.0.1"},
		{"10.192.0.0/10", "10.224.0.1"},
		{"10.224.0.0/11", "10.240.0.1"},
		{"10.240.0.0/12", "10.248.0.1"},
	}
	for i, h := range hops {
		mustUpsert(t, r, newRT(fmt.Sprintf("h%d", i), h.pfx, "address", h.nh, 5, 0, "static"))
	}
	// 终点给一个直连，验证在足够深的上限下可正常到底。
	mustUpsert(t, r, newRT("end", "10.248.0.0/13", "connected", "ethX", 0, 0, "connected"))

	res := r.Lookup(mustAddr(t, "10.0.0.1"))
	if res.Status != StatusDepthExceeded {
		t.Fatalf("status=%s want depth_exceeded; chain=%v", res.Status, chainIDs(res))
	}
	if res.Depth < 3 {
		t.Fatalf("depth=%d expected at least 3 before failing", res.Depth)
	}

	// 同一表、上限放宽到 16，应成功解析。
	deep := New().WithMaxDepth(16)
	for _, rt := range r.Routes(netmodel.AFIPv4) {
		mustUpsert(t, deep, rt)
	}
	res2 := deep.Lookup(mustAddr(t, "10.0.0.1"))
	if res2.Status != StatusForwarded || res2.Egress != "ethX" {
		t.Fatalf("deep lookup status=%s egress=%q chain=%v", res2.Status, res2.Egress, chainIDs(res2))
	}
}

// TestReplaceAtomicVersion：批量替换整表只递增一次版本，
// 且并发读要么看旧表要么看新表，看不到中间态。
func TestReplaceAtomicVersion(t *testing.T) {
	r := New()
	mustUpsert(t, r, newRT("old", "10.0.0.0/8", "blackhole", "", 5, 0, "static"))
	before := r.Version()

	newV4 := []netmodel.Route{
		newRT("new1", "192.0.2.0/24", "connected", "eth9", 0, 0, "connected"),
		newRT("new2", "198.51.100.0/24", "connected", "eth8", 0, 0, "connected"),
	}
	if err := r.ReplaceAll(ReplaceRequest{V4: newV4}); err != nil {
		t.Fatal(err)
	}
	if r.Version() != before+1 {
		t.Fatalf("version jumped %d -> %d, want +1", before, r.Version())
	}
	// 旧前缀必须彻底消失（不是“追加”）。
	if res := r.Lookup(mustAddr(t, "10.1.1.1")); res.Status != StatusNoRoute {
		t.Fatalf("old route survived replace: %s", res.Status)
	}
	if res := r.Lookup(mustAddr(t, "192.0.2.55")); res.Status != StatusForwarded || res.Egress != "eth9" {
		t.Fatalf("new route missing: %s %q", res.Status, res.Egress)
	}

	// 非法批量必须整体拒绝，版本不变。
	goodVersion := r.Version()
	err := r.ReplaceAll(ReplaceRequest{V4: []netmodel.Route{
		newRT("ok", "172.16.0.0/12", "blackhole", "", 5, 0, "static"),
		newRT("bad-af", "10.0.0.0/8", "address", "2001:db8::1", 5, 0, "static"),
	}})
	if err == nil {
		t.Fatal("expected cross-family rejection")
	}
	if r.Version() != goodVersion {
		t.Fatalf("version changed on failed replace: %d -> %d", goodVersion, r.Version())
	}
	if res := r.Lookup(mustAddr(t, "192.0.2.1")); res.Status != StatusForwarded {
		t.Fatal("table mutated by failed replace")
	}
}

// TestConcurrentReadersStable 替换进行中并发读，结果只可能是一致的旧表或新表。
func TestConcurrentReadersStable(t *testing.T) {
	r := New()
	mustUpsert(t, r, newRT("old", "0.0.0.0/0", "connected", "old0", 1, 0, "static"))

	var wg sync.WaitGroup
	stop := make(chan struct{})
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				res := r.Lookup(mustAddr(t, "8.8.8.8"))
				// 旧表 old0；新表 new0 或 no_route（v4 清空），三者都须是终态一致视图。
				switch res.Egress {
				case "old0", "new0":
				default:
					if res.Status != StatusNoRoute {
						t.Errorf("torn read: egress=%q status=%s", res.Egress, res.Status)
					}
				}
			}
		}
	}()
	for i := 0; i < 200; i++ {
		var v4 []netmodel.Route
		if i%2 == 0 {
			v4 = []netmodel.Route{newRT("new", "0.0.0.0/0", "connected", "new0", 1, 0, "static")}
		}
		if err := r.ReplaceAll(ReplaceRequest{V4: v4}); err != nil {
			t.Fatal(err)
		}
	}
	close(stop)
	wg.Wait()
}

func TestUpsertReplaceDeleteCandidates(t *testing.T) {
	r := New()
	p := netmodel.MustPrefix("10.0.0.0/8")
	mustUpsert(t, r,
		newRT("x", "10.0.0.0/8", "connected", "a", 5, 0, "static"),
		newRT("y", "10.0.0.0/8", "connected", "b", 6, 0, "static"),
	)
	// 同 ID 覆盖：候选数不增长。
	mustUpsert(t, r, newRT("x", "10.0.0.0/8", "connected", "a2", 5, 0, "static"))
	routes := r.Routes(netmodel.AFIPv4)
	if len(routes) != 2 {
		t.Fatalf("candidate count=%d want 2", len(routes))
	}
	res := r.Lookup(mustAddr(t, "10.0.0.1"))
	if res.Egress != "a2" {
		t.Fatalf("overwrite did not take: %q", res.Egress)
	}
	// 删除一个候选，另一个仍在。
	if err := r.Delete(p, "x"); err != nil {
		t.Fatal(err)
	}
	res = r.Lookup(mustAddr(t, "10.0.0.1"))
	if res.Egress != "b" {
		t.Fatalf("after delete x, expected remaining candidate b, got %q", res.Egress)
	}
	// 删光候选后节点消失。
	if err := r.Delete(p, "y"); err != nil {
		t.Fatal(err)
	}
	if err := r.Delete(p, "y"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("double delete err=%v want ErrNotFound", err)
	}
	if res := r.Lookup(mustAddr(t, "10.0.0.1")); res.Status != StatusNoRoute {
		t.Fatalf("node not removed: %s", res.Status)
	}
}

// TestSnapshotAcrossReplace：批量替换前取得的查询结果不受之后替换影响
// （Lookup 内部即基于快照；这里直接验证版本化行为）。
func TestVersionMonotonic(t *testing.T) {
	r := New()
	v0 := r.Version()
	mustUpsert(t, r, newRT("a", "10.0.0.0/8", "blackhole", "", 5, 0, "static"))
	mustUpsert(t, r, newRT("b", "11.0.0.0/8", "blackhole", "", 5, 0, "static"))
	if err := r.Delete(netmodel.MustPrefix("10.0.0.0/8"), "a"); err != nil {
		t.Fatal(err)
	}
	_ = r.ReplaceAll(ReplaceRequest{})
	if r.Version() <= v0+3 {
		t.Fatalf("version not monotonic: start=%d now=%d", v0, r.Version())
	}
}
