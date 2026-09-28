// Package rib 是路由信息库核心：维护按地址族隔离的两套前缀树，
// 实现“最长前缀优先，其次管理距离”的确定性选路、递归下一跳解析、
// 递归环与深度上限检测，以及单表版本下原子可见的批量替换。
package rib

import (
	"errors"
	"fmt"
	"net/netip"
	"sort"
	"sync"

	"rib/internal/netmodel"
	"rib/internal/trie"
)

// 默认递归解析深度上限（跳数），可在 New 时覆盖。
const DefaultMaxDepth = 16

// 失败类别：测试与诊断都按这些稳定常量断言，而不是字符串匹配。
type Status string

const (
	// StatusForwarded 解析成功，终点为直连（或带地址但已到底的语义）。
	StatusForwarded Status = "forwarded"
	// StatusBlackhole 终点为黑洞路由。
	StatusBlackhole Status = "blackhole"
	// StatusUnreachable 终点为 unreachable 路由。
	StatusUnreachable Status = "unreachable"
	// StatusNoRoute 没有任何匹配前缀（含默认路由）。
	StatusNoRoute Status = "no_route"
	// StatusNexthopUnresolved 选中了递归下一跳，但下一跳地址无法再解析。
	StatusNexthopUnresolved Status = "nexthop_unresolved"
	// StatusRecursionLoop 解析过程中再次进入已访问过的前缀（环）。
	StatusRecursionLoop Status = "recursion_loop"
	// StatusDepthExceeded 递归跳数超过 MaxDepth。
	StatusDepthExceeded Status = "depth_exceeded"
)

// ErrNotFound 在删除不存在的路由时返回，便于调用方映射 404。
var ErrNotFound = errors.New("route not found")

// ChainHop 是匹配链上的一跳：查询地址在该跳命中的前缀、被选中的候选
// 以及该跳选择它的原因。输出“匹配链”是诊断与测试对照的核心。
type ChainHop struct {
	Prefix string         `json:"prefix"`
	Chosen netmodel.Route `json:"chosen"`
	Reason string         `json:"reason"`
}

// LookupResult 是一次解析的完整结果。
type LookupResult struct {
	Status Status     `json:"status"`
	Family string     `json:"family"`
	Target string     `json:"target"`
	Egress string     `json:"egress,omitempty"`
	Chain  []ChainHop `json:"chain"`
	Depth  int        `json:"depth"`
	// Diag 记录关键内部状态（候选数、访问序、为何接受/拒绝/无法判定）。
	Diag []string `json:"diagnostics,omitempty"`
}

// ProtocolPreference 定义固定的协议先后（决胜最后一级使用）。
// 未列出的协议排在末尾，再按协议名字典序，保证结果确定。
var ProtocolPreference = map[string]int{
	"connected": 0,
	"static":    1,
	"ospf":      2,
	"isis":      3,
	"bgp":       4,
}

// RIB 是并发安全的路由信息库。
type RIB struct {
	mu       sync.RWMutex
	v4       *trie.Trie[[]netmodel.Route]
	v6       *trie.Trie[[]netmodel.Route]
	version  int64
	maxDepth int
}

// New 创建空 RIB。
func New() *RIB {
	return &RIB{
		v4:       trie.New[[]netmodel.Route](),
		v6:       trie.New[[]netmodel.Route](),
		version:  1,
		maxDepth: DefaultMaxDepth,
	}
}

// WithMaxDepth 设置递归深度上限（主要供深度上限测试使用）。
func (r *RIB) WithMaxDepth(d int) *RIB {
	r.maxDepth = d
	return r
}

// Version 返回当前表版本号：任何变更单调递增；批量替换只递增一次。
func (r *RIB) Version() int64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.version
}

// Snapshot 是某一表版本的只读视图。Lookup 基于快照完成，
// 因而无需长时间持锁，且与并发写入互不干扰。
type Snapshot struct {
	v4, v6   *trie.Trie[[]netmodel.Route]
	version  int64
	maxDepth int
}

// snapshot 取当前两棵树的 COW 快照。
func (r *RIB) snapshot() Snapshot {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return Snapshot{v4: r.v4.Snapshot(), v6: r.v6.Snapshot(), version: r.version, maxDepth: r.maxDepth}
}

func prefixBits(p netmodel.Prefix) ([]byte, int) {
	return p.Addr().AsSlice(), p.Bits()
}

func addrBits(a netip.Addr) []byte {
	if a.Is4In6() {
		a = a.Unmap()
	}
	return a.AsSlice()
}

// tree 按族选取树（仅在已持锁或快照上调用）。
func (r *RIB) tree(f netmodel.Family) *trie.Trie[[]netmodel.Route] {
	if f == netmodel.AFIPv6 {
		return r.v6
	}
	return r.v4
}

// Upsert 插入或替换同 (前缀,ID) 的路由。同一前缀不同 ID 作为并列候选。
// 路由在写入前完成自洽校验（含跨族下一跳拒绝）。
func (r *RIB) Upsert(route netmodel.Route) error {
	if err := route.Validate(); err != nil {
		return err
	}
	key, bits := prefixBits(route.Prefix)
	r.mu.Lock()
	defer r.mu.Unlock()

	t := r.tree(route.Prefix.Family())
	candidates, _ := t.Get(key, bits)
	next := make([]netmodel.Route, 0, len(candidates)+1)
	replaced := false
	for _, c := range candidates {
		if c.ID == route.ID {
			next = append(next, route)
			replaced = true
		} else {
			next = append(next, c)
		}
	}
	if !replaced {
		next = append(next, route)
	}
	t.Put(key, bits, next)
	r.version++
	return nil
}

// Delete 删除指定前缀下同 ID 的候选；该前缀无候选时树节点被压缩回收。
func (r *RIB) Delete(p netmodel.Prefix, id string) error {
	key, bits := prefixBits(p)
	r.mu.Lock()
	defer r.mu.Unlock()

	t := r.tree(p.Family())
	candidates, ok := t.Get(key, bits)
	if !ok {
		return ErrNotFound
	}
	next := candidates[:0:0]
	found := false
	for _, c := range candidates {
		if c.ID == id {
			found = true
			continue
		}
		next = append(next, c)
	}
	if !found {
		return ErrNotFound
	}
	if len(next) == 0 {
		t.Delete(key, bits)
	} else {
		t.Put(key, bits, next)
	}
	r.version++
	return nil
}

// ReplaceRequest 是一次整表批量替换。空前缀列表表示清空该族。
type ReplaceRequest struct {
	V4 []netmodel.Route
	V6 []netmodel.Route
}

// ReplaceAll 原子地重建两族前缀树。所有路由先校验、建树，
// 成功后才整体换根并只递增一次版本——外部观察者要么看到旧表，
// 要么看到完整新表，绝不会看到中间态。任一路由非法时整体拒绝、
// 表与版本均不变。
func (r *RIB) ReplaceAll(req ReplaceRequest) error {
	nv4 := trie.New[[]netmodel.Route]()
	nv6 := trie.New[[]netmodel.Route]()
	if err := fillTree(nv4, req.V4, netmodel.AFIPv4); err != nil {
		return err
	}
	if err := fillTree(nv6, req.V6, netmodel.AFIPv6); err != nil {
		return err
	}
	r.mu.Lock()
	r.v4, r.v6 = nv4, nv6
	r.version++
	r.mu.Unlock()
	return nil
}

func fillTree(t *trie.Trie[[]netmodel.Route], routes []netmodel.Route, want netmodel.Family) error {
	seen := map[string]bool{}
	for _, rt := range routes {
		if err := rt.Validate(); err != nil {
			return fmt.Errorf("route %q: %w", rt.ID, err)
		}
		if rt.Prefix.Family() != want {
			return fmt.Errorf("route %q: %w (want %s, got %s)", rt.ID, netmodel.ErrAFMismatch, want, rt.Prefix.Family())
		}
		key := rt.Prefix.String() + "#" + rt.ID
		if seen[key] {
			return fmt.Errorf("route %q: duplicate id within prefix %s", rt.ID, rt.Prefix.String())
		}
		seen[key] = true

		bk, bits := prefixBits(rt.Prefix)
		existing, _ := t.Get(bk, bits)
		t.Put(bk, bits, append(existing, rt))
	}
	return nil
}

// Routes 返回当前某族全部路由（诊断/重放比对用），顺序按前缀与 ID 排序。
func (r *RIB) Routes(f netmodel.Family) []netmodel.Route {
	snap := r.snapshot()
	t := snap.v4
	if f == netmodel.AFIPv6 {
		t = snap.v6
	}
	var out []netmodel.Route
	t.Walk(func(cands []netmodel.Route) bool {
		out = append(out, cands...)
		return true
	})
	sort.Slice(out, func(i, j int) bool {
		if out[i].Prefix.String() != out[j].Prefix.String() {
			return out[i].Prefix.Bits() < out[j].Prefix.Bits() ||
				(out[i].Prefix.Bits() == out[j].Prefix.Bits() &&
					out[i].Prefix.Addr().Less(out[j].Prefix.Addr()))
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// choose 对同前缀候选执行固定策略选路。
// 顺序：管理距离 < metric < 协议固定先后 < 下一跳地址字节 < ID。
// （掩码长度比较已由 LPM 在更外层完成：能进入这里的候选必然同前缀。）
func choose(candidates []netmodel.Route) (netmodel.Route, string) {
	best := candidates[0]
	reason := "only candidate"
	for _, c := range candidates[1:] {
		better, why := beats(c, best)
		if better {
			best, reason = c, why
		}
	}
	return best, reason
}

// beats 返回 a 是否应当优先于 b，以及原因（稳定文本，供匹配链展示）。
func beats(a, b netmodel.Route) (bool, string) {
	switch {
	case a.AdminDistance != b.AdminDistance:
		return a.AdminDistance < b.AdminDistance,
			fmt.Sprintf("admin_distance %d < %d", a.AdminDistance, b.AdminDistance)
	case a.Metric != b.Metric:
		return a.Metric < b.Metric,
			fmt.Sprintf("metric %d < %d", a.Metric, b.Metric)
	case protoRank(a.Protocol) != protoRank(b.Protocol):
		return protoRank(a.Protocol) < protoRank(b.Protocol),
			fmt.Sprintf("protocol %s preferred over %s", a.Protocol, b.Protocol)
	case a.Nexthop.HasAddress() != b.Nexthop.HasAddress():
		// 带地址与不带地址同为终局时，优先带地址的仅用于确定性，
		// 实践中同前缀终局类型通常一致。
		return a.Nexthop.HasAddress(), "address-bearing nexthop preferred"
	case a.Nexthop.HasAddress() && *a.Nexthop.Address != *b.Nexthop.Address:
		return addrLess(*a.Nexthop.Address, *b.Nexthop.Address), "lower nexthop address"
	default:
		return a.ID < b.ID, fmt.Sprintf("route id %q < %q", a.ID, b.ID)
	}
}

func protoRank(p string) int {
	if n, ok := ProtocolPreference[p]; ok {
		return n
	}
	if p == "" {
		return 50
	}
	// 未知协议：首字节给出确定性粗排，真正并列时由后续键决定。
	return 100 + int(p[0])
}

func addrLess(a, b netip.Addr) bool {
	ab, bb := addrBits(a), addrBits(b)
	for i := 0; i < len(ab) && i < len(bb); i++ {
		if ab[i] != bb[i] {
			return ab[i] < bb[i]
		}
	}
	return len(ab) < len(bb)
}

// Lookup 对目标地址做完整解析并返回匹配链。
func (r *RIB) Lookup(target netip.Addr) LookupResult {
	return r.snapshot().lookup(target)
}

func (s Snapshot) treeFor(f netmodel.Family) *trie.Trie[[]netmodel.Route] {
	if f == netmodel.AFIPv6 {
		return s.v6
	}
	return s.v4
}

func (s Snapshot) lookup(target netip.Addr) LookupResult {
	fam := netmodel.FamilyOfAddr(target)
	res := LookupResult{
		Family: fam.String(),
		Target: target.String(),
		Chain:  []ChainHop{},
		Diag:   []string{},
	}
	if fam == netmodel.AFUnspecified {
		res.Status = StatusNoRoute
		res.Diag = append(res.Diag, "reject: target address is unspecified")
		return res
	}

	tree := s.treeFor(fam)

	// visited 以“前缀#路由ID”为键检测递归环（同一路由条目不可重复进入）。
	visited := map[string]bool{}
	currentAddr := target

	for {
		candidates, ok := tree.LongestPrefix(addrBits(currentAddr))
		if !ok {
			if len(res.Chain) == 0 {
				res.Status = StatusNoRoute
				res.Diag = append(res.Diag,
					"undecidable: no covering prefix (including default) for "+currentAddr.String())
			} else {
				res.Status = StatusNexthopUnresolved
				res.Diag = append(res.Diag,
					"reject: nexthop "+currentAddr.String()+" has no covering route")
			}
			return res
		}

		chosen, reason := choose(candidates)
		hop := ChainHop{Prefix: chosen.Prefix.String(), Chosen: chosen, Reason: reason}
		entryKey := chosen.Prefix.String() + "#" + chosen.ID
		res.Diag = append(res.Diag, fmt.Sprintf(
			"accept: %d candidate(s) on %s, selected id=%s (%s)",
			len(candidates), chosen.Prefix.String(), chosen.ID, reason))

		// 进入该条目前先判环：环检测优先于深度计数，保证任何环都报 recursion_loop。
		if visited[entryKey] {
			res.Chain = append(res.Chain, hop)
			res.Status = StatusRecursionLoop
			res.Depth = len(res.Chain)
			res.Diag = append(res.Diag,
				"reject: recursion loop re-enters "+entryKey+
					"; visit order="+visitList(visited)+" -> "+entryKey)
			return res
		}
		visited[entryKey] = true
		res.Chain = append(res.Chain, hop)
		res.Depth = len(res.Chain)

		switch chosen.Nexthop.Kind {
		case netmodel.NHConnected:
			res.Status = StatusForwarded
			res.Egress = chosen.Nexthop.Iface
			res.Diag = append(res.Diag, "accept: terminating connected nexthop via "+chosen.Nexthop.Iface)
			return res
		case netmodel.NHBlackhole:
			res.Status = StatusBlackhole
			res.Diag = append(res.Diag, "reject: terminating blackhole nexthop")
			return res
		case netmodel.NHUnreachable:
			res.Status = StatusUnreachable
			res.Diag = append(res.Diag, "undecidable: terminating unreachable nexthop")
			return res
		case netmodel.NHAddress:
			nh := *chosen.Nexthop.Address
			if nh.Is4In6() {
				nh = nh.Unmap()
			}
			res.Diag = append(res.Diag, "recurse: follow nexthop "+nh.String())
			if res.Depth >= s.maxDepth {
				res.Status = StatusDepthExceeded
				res.Diag = append(res.Diag,
					fmt.Sprintf("reject: recursion depth %d exceeds limit %d before resolving %s",
						res.Depth, s.maxDepth, nh.String()))
				return res
			}
			currentAddr = nh
		default:
			res.Status = StatusNexthopUnresolved
			res.Diag = append(res.Diag, "undecidable: unknown nexthop kind")
			return res
		}
	}
}

func visitList(visited map[string]bool) string {
	out := make([]string, 0, len(visited))
	for k := range visited {
		out = append(out, k)
	}
	sort.Strings(out)
	join := ""
	for i, k := range out {
		if i > 0 {
			join += ","
		}
		join += k
	}
	return join
}
