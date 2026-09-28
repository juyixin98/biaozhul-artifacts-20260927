package acceptance

import (
	"net/netip"
	"sort"
	"testing"

	"rib/internal/netmodel"
)

// referenceResult 是独立参考解析器的输出，字段与 golden 一一对应。
type referenceResult struct {
	status        string
	egress        string
	depth         int
	chainIDs      []string
	chainPrefixes []string
}

// referenceResolver 是与被测核心完全独立的朴素实现：
//   - LPM：对全部路由线性扫描，选掩码最长的覆盖前缀；
//   - 同前缀选路：按固定键序排序取首条；
//   - 递归：显式 for 循环 + 已访问集合 + 深度计数。
//
// 它刻意写成与 trie/rib 不同的算法（无树、无快照、无压缩），
// 只共享网络模型的“解析/规范化”原语，作为独立参照。
type referenceResolver struct {
	v4, v6   []netmodel.Route
	maxDepth int
}

func newReferenceResolver(routes []netmodel.Route, maxDepth int) *referenceResolver {
	rr := &referenceResolver{maxDepth: maxDepth}
	for _, rt := range routes {
		if rt.Prefix.Family() == netmodel.AFIPv6 {
			rr.v6 = append(rr.v6, rt)
		} else {
			rr.v4 = append(rr.v4, rt)
		}
	}
	// 预排序：同组（同前缀）候选的首条即固定策略胜者。
	rr.v4 = sortCandidates(rr.v4)
	rr.v6 = sortCandidates(rr.v6)
	return rr
}

// sortCandidates 按 (前缀字符串, 策略键) 排序，使同前缀候选连续、首条最优。
func sortCandidates(rs []netmodel.Route) []netmodel.Route {
	out := append([]netmodel.Route(nil), rs...)
	sort.SliceStable(out, func(i, j int) bool {
		pi, pj := out[i].Prefix.String(), out[j].Prefix.String()
		if pi != pj {
			return pi < pj
		}
		return policyLess(out[i], out[j])
	})
	return out
}

// policyLess 复刻固定策略：AD < metric < 协议 < 下一跳地址 < ID。
// 顺序定义直接取自需求，不读取 rib 包中的任何代码。
func policyLess(a, b netmodel.Route) bool {
	if a.AdminDistance != b.AdminDistance {
		return a.AdminDistance < b.AdminDistance
	}
	if a.Metric != b.Metric {
		return a.Metric < b.Metric
	}
	ra, rb := protoOrder(a.Protocol), protoOrder(b.Protocol)
	if ra != rb {
		return ra < rb
	}
	aa := a.Nexthop.HasAddress()
	ab := b.Nexthop.HasAddress()
	if aa != ab {
		return aa
	}
	if aa && a.Nexthop.Address.String() != b.Nexthop.Address.String() {
		return a.Nexthop.Address.Less(*b.Nexthop.Address)
	}
	return a.ID < b.ID
}

func protoOrder(p string) int {
	switch p {
	case "connected":
		return 0
	case "static":
		return 1
	case "ospf":
		return 2
	case "isis":
		return 3
	case "bgp":
		return 4
	case "":
		return 50
	default:
		return 100 + int(p[0])
	}
}

func (rr *referenceResolver) tableFor(f netmodel.Family) []netmodel.Route {
	if f == netmodel.AFIPv6 {
		return rr.v6
	}
	return rr.v4
}

// lpm 线性最长前缀匹配，并返回同前缀最优候选。
func (rr *referenceResolver) lpm(addr netip.Addr) (netmodel.Route, bool) {
	var best *netmodel.Route
	bestBits := -1
	table := rr.tableFor(netmodel.FamilyOfAddr(addr))
	for i := range table {
		rt := table[i]
		if rt.Prefix.Contains(addr) && rt.Prefix.Bits() > bestBits {
			bestBits = rt.Prefix.Bits()
			cp := rt
			best = &cp
		}
	}
	if best == nil {
		return netmodel.Route{}, false
	}
	// 同前缀候选在排序后首条最优；由于扫描顺序即排序顺序，
	// 第一个以 bestBits 命中的就是最优者，这里再做一次显式选择以防假设错误。
	winner := *best
	for i := range table {
		rt := table[i]
		if rt.Prefix.Bits() == bestBits && rt.Prefix.Contains(addr) && policyLess(rt, winner) {
			winner = rt
		}
	}
	return winner, true
}

func (rr *referenceResolver) resolve(target netip.Addr) referenceResult {
	res := referenceResult{chainIDs: []string{}, chainPrefixes: []string{}}
	if netmodel.FamilyOfAddr(target) == netmodel.AFUnspecified {
		res.status = "no_route"
		return res
	}

	visited := map[string]bool{}
	cur := target
	for {
		rt, ok := rr.lpm(cur)
		if !ok {
			if len(res.chainIDs) == 0 {
				res.status = "no_route"
			} else {
				res.status = "nexthop_unresolved"
			}
			return res
		}
		key := rt.Prefix.String() + "#" + rt.ID
		res.chainIDs = append(res.chainIDs, rt.ID)
		res.chainPrefixes = append(res.chainPrefixes, rt.Prefix.String())
		res.depth = len(res.chainIDs)

		if visited[key] {
			res.status = "recursion_loop"
			return res
		}
		visited[key] = true

		switch rt.Nexthop.Kind {
		case netmodel.NHConnected:
			res.status = "forwarded"
			res.egress = rt.Nexthop.Iface
			return res
		case netmodel.NHBlackhole:
			res.status = "blackhole"
			return res
		case netmodel.NHUnreachable:
			res.status = "unreachable"
			return res
		case netmodel.NHAddress:
			if res.depth >= rr.maxDepth {
				res.status = "depth_exceeded"
				return res
			}
			cur = rt.Nexthop.Address.Unmap()
		default:
			res.status = "nexthop_unresolved"
			return res
		}
	}
}

func assertRefResult(t *testing.T, tag string, got referenceResult, want referenceResult) {
	t.Helper()
	if got.status != want.status {
		t.Fatalf("%s: status=%s want %s", tag, got.status, want.status)
	}
	if got.egress != want.egress {
		t.Fatalf("%s: egress=%q want %q", tag, got.egress, want.egress)
	}
	if got.depth != want.depth {
		t.Fatalf("%s: depth=%d want %d", tag, got.depth, want.depth)
	}
	if !eqStrings(got.chainIDs, want.chainIDs) {
		t.Fatalf("%s: chain_ids=%v want %v", tag, got.chainIDs, want.chainIDs)
	}
	if !eqStrings(got.chainPrefixes, want.chainPrefixes) {
		t.Fatalf("%s: chain_prefixes=%v want %v", tag, got.chainPrefixes, want.chainPrefixes)
	}
}

func eqStrings(a, b []string) bool {
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

// ---- 夹具构造辅助（API 与处理器/DTO 无关的纯模型构造）----

func parseTarget(t *testing.T, s string) netip.Addr {
	t.Helper()
	a, err := netip.ParseAddr(s)
	if err != nil {
		t.Fatal(err)
	}
	return a.Unmap()
}

func decodeRoute(id, prefix string, ad, metric int, protocol, kind, addr, iface string) (netmodel.Route, error) {
	p, err := netmodel.ParsePrefix(prefix)
	if err != nil {
		return netmodel.Route{}, err
	}
	nh := netmodel.Nexthop{Kind: netmodel.NHKind(kind), Iface: iface}
	if addr != "" {
		a, err := netip.ParseAddr(addr)
		if err != nil {
			return netmodel.Route{}, err
		}
		a = a.Unmap()
		nh.Address = &a
	}
	rt := netmodel.Route{
		ID: id, Prefix: p, Nexthop: nh,
		AdminDistance: ad, Metric: metric, Protocol: protocol,
	}
	return rt, rt.Validate()
}
