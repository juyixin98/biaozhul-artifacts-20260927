package acceptance

import (
	"errors"
	"fmt"
	"math/rand"
	"net/netip"
	"testing"

	"rib/internal/netmodel"
	"rib/internal/rib"
)

// TestRandomEquivalenceWithReference 在随机 IPv4 表上让被测核心与
// 独立朴素参考解析器逐字段对拍。随机表刻意包含：默认路由、主机路由、
// 重叠前缀、同前缀多候选（不同 AD/metric/protocol/下一跳）、直连/黑洞、
// 悬空递归下一跳、以及概率性环。
func TestRandomEquivalenceWithReference(t *testing.T) {
	rng := rand.New(rand.NewSource(20260928))
	for iter := 0; iter < 300; iter++ {
		routes := genRandomTable(t, rng)
		// 表版本（环概率）下深度上限随机。
		maxDepth := 2 + rng.Intn(14)
		core := rib.New().WithMaxDepth(maxDepth)
		for _, rt := range routes {
			if err := core.Upsert(rt); err != nil {
				t.Fatalf("iter %d upsert: %v", iter, err)
			}
		}
		ref := newReferenceResolver(routes, maxDepth)

		// 既探测随机地址，也探测每个下一跳地址（递归路径覆盖更好）。
		probes := randomProbes(rng, 40)
		for _, rt := range routes {
			if rt.Nexthop.HasAddress() {
				probes = append(probes, rt.Nexthop.Address.Unmap())
			}
		}
		for _, target := range probes {
			got := core.Lookup(target)
			want := ref.resolve(target)

			if string(got.Status) != want.status {
				t.Fatalf("iter %d target=%s status core=%s ref=%s\ncore=%v\nref=%v",
					iter, target, got.Status, want.status, got.Chain, want.chainIDs)
			}
			if got.Egress != want.egress {
				t.Fatalf("iter %d target=%s egress core=%q ref=%q", iter, target, got.Egress, want.egress)
			}
			if got.Depth != want.depth {
				t.Fatalf("iter %d target=%s depth core=%d ref=%d (status=%s)",
					iter, target, got.Depth, want.depth, got.Status)
			}
			cIDs, cPfx := coreChains(got)
			if !eqStrings(cIDs, want.chainIDs) {
				t.Fatalf("iter %d target=%s chain ids core=%v ref=%v", iter, target, cIDs, want.chainIDs)
			}
			if !eqStrings(cPfx, want.chainPrefixes) {
				t.Fatalf("iter %d target=%s chain pfx core=%v ref=%v", iter, target, cPfx, want.chainPrefixes)
			}
		}
	}
}

func coreChains(got rib.LookupResult) ([]string, []string) {
	ids := make([]string, len(got.Chain))
	pfx := make([]string, len(got.Chain))
	for i, h := range got.Chain {
		ids[i] = h.Chosen.ID
		pfx[i] = h.Prefix
	}
	return ids, pfx
}

// genRandomTable 生成一张自洽的随机 IPv4 路由表。
func genRandomTable(t *testing.T, rng *rand.Rand) []netmodel.Route {
	t.Helper()
	var routes []netmodel.Route

	// 若干固定“骨干”下一跳子网：默认 + 一个直连 /24，作为递归终点。
	edge := netmodel.Route{
		ID: "edge", Prefix: netmodel.MustPrefix("203.0.113.0/24"),
		AdminDistance: 0, Metric: 0, Protocol: "connected",
		Nexthop: netmodel.Nexthop{Kind: netmodel.NHConnected, Iface: "eth0"},
	}
	routes = append(routes, edge)

	// 默认路由以 70% 概率出现（制造 no_route 场景）。
	if rng.Intn(10) < 7 {
		routes = append(routes, newModelRT(t, "default", "0.0.0.0/0",
			"address", "203.0.113.1", rng.Intn(200)+10, rng.Intn(100), "static"))
	}

	// 随机前缀：长度取自 {8,16,24,32}，地址随机，主机位由 ParsePrefix 清零。
	n := 6 + rng.Intn(10)
	usedIDs := map[string]bool{"edge": true, "default": true}
	protos := []string{"static", "ospf", "bgp", "isis"}
	for i := 0; i < n; i++ {
		bits := []int{8, 16, 24, 32}[rng.Intn(4)]
		pfx := randPrefix(rng, bits)

		id := uniqueID(rng, usedIDs)
		switch rng.Intn(10) {
		case 0, 1: // 直连终局
			routes = append(routes, newModelRT(t, id, pfx, "connected",
				fmt.Sprintf("eth%d", rng.Intn(4)), rng.Intn(6), rng.Intn(100),
				protos[rng.Intn(len(protos))]))
		case 2: // 黑洞/不可达
			kind := "blackhole"
			if rng.Intn(2) == 1 {
				kind = "unreachable"
			}
			routes = append(routes, newModelRT(t, id, pfx, kind, "",
				rng.Intn(200), rng.Intn(100), "static"))
		default: // 递归下一跳：多数指向可解析区域，小概率悬空或环。
			var nh string
			switch rng.Intn(10) {
			case 0: // 悬空：RFC2544 198.18.0.0/15 区域，不保证有覆盖
				nh = fmt.Sprintf("198.18.%d.%d", rng.Intn(256), rng.Intn(254)+1)
			case 1: // 指回随机已生成/未来前缀地址，制造环的概率
				nh = randAddrInPrefix(rng, pfx)
			default:
				nh = fmt.Sprintf("203.0.113.%d", rng.Intn(254)+1)
			}
			routes = append(routes, newModelRT(t, id, pfx, "address", nh,
				rng.Intn(256), rng.Intn(100), protos[rng.Intn(len(protos))]))
		}
	}

	// 再以 50% 概率给某随机前缀加 1~2 个同前缀竞争候选（测策略决胜）。
	if rng.Intn(2) == 0 && len(routes) > 2 {
		base := routes[rng.Intn(len(routes))]
		for k := 0; k < 1+rng.Intn(2); k++ {
			id := uniqueID(rng, usedIDs)
			// 竞争候选使用不同下一跳地址/接口，避免完全相同。
			if base.Nexthop.Kind == netmodel.NHConnected {
				routes = append(routes, newModelRT(t, id, base.Prefix.String(),
					"connected", fmt.Sprintf("eth%d", rng.Intn(4)),
					rng.Intn(256), rng.Intn(100), protos[rng.Intn(len(protos))]))
			} else if base.Nexthop.Kind == netmodel.NHAddress {
				nh := fmt.Sprintf("203.0.113.%d", rng.Intn(254)+1)
				routes = append(routes, newModelRT(t, id, base.Prefix.String(),
					"address", nh, rng.Intn(256), rng.Intn(100),
					protos[rng.Intn(len(protos))]))
			}
		}
	}

	return routes
}

func uniqueID(rng *rand.Rand, used map[string]bool) string {
	for {
		id := fmt.Sprintf("r%06x", rng.Intn(1<<24))
		if !used[id] {
			used[id] = true
			return id
		}
	}
}

func randPrefix(rng *rand.Rand, bits int) string {
	b := make([]byte, 4)
	rng.Read(b)
	return fmt.Sprintf("%d.%d.%d.%d/%d", b[0], b[1], b[2], b[3], bits)
}

func randAddrInPrefix(rng *rand.Rand, pfx string) string {
	p, _ := netmodel.ParsePrefix(pfx)
	b := p.Addr().As4()
	// 主机位随机化。
	for i := p.Bits(); i < 32; i++ {
		if rng.Intn(2) == 1 {
			b[i/8] |= 1 << (7 - uint(i%8))
		}
	}
	addr := netip.AddrFrom4(b)
	if p.Contains(addr) && !addr.IsUnspecified() {
		return addr.String()
	}
	return fmt.Sprintf("203.0.113.%d", rng.Intn(254)+1)
}

func randomProbes(rng *rand.Rand, n int) []netip.Addr {
	out := make([]netip.Addr, 0, n)
	for i := 0; i < n; i++ {
		b := make([]byte, 4)
		rng.Read(b)
		out = append(out, netip.AddrFrom4([4]byte{b[0], b[1], b[2], b[3]}).Unmap())
	}
	return out
}

func newModelRT(t *testing.T, id, pfx, kind, nhOrIface string, ad, metric int, proto string) netmodel.Route {
	t.Helper()
	var addr, iface string
	switch netmodel.NHKind(kind) {
	case netmodel.NHAddress:
		addr = nhOrIface
	case netmodel.NHConnected:
		iface = nhOrIface
	}
	rt, err := decodeRoute(id, pfx, ad, metric, proto, kind, addr, iface)
	if err != nil {
		t.Fatalf("fixture route %s invalid: %v", id, err)
	}
	return rt
}

// TestValidationFailureCategories 断言入口校验拒绝的具体失败类别，
// 而不是笼统的“返回了错误”。
func TestValidationFailureCategories(t *testing.T) {
	valid := func() netmodel.Route {
		return newModelRT(t, "ok", "10.0.0.0/8", "address", "10.0.0.1", 5, 0, "static")
	}
	mustErr := func(name string, mutate func(*netmodel.Route), target error) {
		t.Helper()
		r := rib.New()
		rt := valid()
		mutate(&rt)
		err := r.Upsert(rt)
		if !errors.Is(err, target) {
			t.Errorf("%s: err=%v want %v", name, err, target)
		}
	}

	mustErr("cross family nexthop", func(rt *netmodel.Route) {
		a, _ := netip.ParseAddr("2001:db8::1")
		rt.Nexthop.Address = &a
	}, netmodel.ErrAFMismatch)

	// 非法前缀在解析阶段即失败（连 RIB 都进不去）。
	if _, err := netmodel.ParsePrefix("10.0.0.0/40"); !errors.Is(err, netmodel.ErrInvalidPrefix) {
		t.Fatalf("oversized mask err=%v", err)
	}
	if _, err := netmodel.ParsePrefix("2001:db8::/129"); !errors.Is(err, netmodel.ErrInvalidPrefix) {
		t.Fatalf("oversized v6 mask err=%v", err)
	}

	mustErr("empty id", func(rt *netmodel.Route) { rt.ID = "" }, netmodel.ErrEmptyID)
	mustErr("bad distance", func(rt *netmodel.Route) { rt.AdminDistance = 999 }, netmodel.ErrBadDistance)
	mustErr("bad metric", func(rt *netmodel.Route) { rt.Metric = -5 }, netmodel.ErrBadMetric)
	mustErr("missing nh address", func(rt *netmodel.Route) { rt.Nexthop.Address = nil }, netmodel.ErrNHAddrRequired)
	mustErr("blackhole with address", func(rt *netmodel.Route) {
		a, _ := netip.ParseAddr("10.0.0.9")
		rt.Nexthop = netmodel.Nexthop{Kind: netmodel.NHBlackhole, Address: &a}
	}, netmodel.ErrNHAddrNotAllowed)
	mustErr("connected without iface", func(rt *netmodel.Route) {
		rt.Nexthop = netmodel.Nexthop{Kind: netmodel.NHConnected}
	}, netmodel.ErrNHIfaceRequired)

	// 重复 ID 在批量替换内必须报错（同前缀同 ID）。
	r := rib.New()
	a := valid()
	b := valid()
	err := r.ReplaceAll(rib.ReplaceRequest{V4: []netmodel.Route{a, b}})
	if err == nil {
		t.Fatal("duplicate id within prefix must be rejected")
	}
}

// TestGoldenFixtureSelfConsistent 是夹具自检：golden 里声明的前缀
// 经规范化后必须与 check 里 chain_prefixes 的预期一致，防止参考答案
// 自身写成了非规范形式。
func TestGoldenFixtureSelfConsistent(t *testing.T) {
	gf := loadGolden(t)
	for _, sc := range gf.Scenarios {
		canonByID := map[string]string{}
		seen := map[string]bool{}
		for _, gr := range sc.Routes {
			p, err := netmodel.ParsePrefix(gr.Prefix)
			if err != nil {
				t.Fatalf("scenario %s route %s bad prefix: %v", sc.Name, gr.ID, err)
			}
			key := gr.ID + "@" + p.String()
			if seen[key] {
				t.Fatalf("scenario %s duplicate route id+prefix %s", sc.Name, key)
			}
			seen[key] = true
			canonByID[gr.ID] = p.String()
		}
		for _, ck := range sc.Checks {
			if len(ck.ChainIDs) != len(ck.ChainPrefixes) {
				t.Fatalf("scenario %s check %q: chain ids/prefixes length mismatch",
					sc.Name, ck.Name)
			}
			for i, id := range ck.ChainIDs {
				if canonByID[id] != ck.ChainPrefixes[i] {
					t.Fatalf("scenario %s check %q: golden prefix for %s is %q, answer says %q",
						sc.Name, ck.Name, id, canonByID[id], ck.ChainPrefixes[i])
				}
			}
		}
	}
}
