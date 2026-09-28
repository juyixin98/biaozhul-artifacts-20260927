// Package acceptance 是独立验收测试：它不导入被测核心 rib 包的任何
// 选路逻辑，而是自带一个朴素、直白的参考解析器（线性 LPM + 固定策略 +
// 显式递归），再用两套断言约束被测系统：
//  1. 手写 golden 夹具（testdata/golden/cases.json）逐项匹配参考答案，
//     断言状态、完整匹配链（ID 与规范化前缀）、深度、出接口、失败类别；
//  2. 随机生成的路由表上，被测 RIB 的结果必须与独立参考解析器逐字段一致。
//
// golden 参考答案由人手工推导（见文件内注释），不是由被测代码生成。
package acceptance

import (
	"encoding/json"
	"os"
	"testing"

	"rib/internal/netmodel"
	"rib/internal/rib"
)

type goldenNH struct {
	Kind      string `json:"kind"`
	Address   string `json:"address"`
	Interface string `json:"interface"`
}

type goldenRoute struct {
	ID            string   `json:"id"`
	Prefix        string   `json:"prefix"`
	AdminDistance int      `json:"admin_distance"`
	Metric        int      `json:"metric"`
	Protocol      string   `json:"protocol"`
	Nexthop       goldenNH `json:"nexthop"`
}

type goldenCheck struct {
	Name          string   `json:"name"`
	Target        string   `json:"target"`
	Status        string   `json:"status"`
	Egress        string   `json:"egress"`
	ChainIDs      []string `json:"chain_ids"`
	ChainPrefixes []string `json:"chain_prefixes"`
	Depth         int      `json:"depth"`
}

type goldenScenario struct {
	Name     string        `json:"name"`
	MaxDepth int           `json:"max_depth"`
	Routes   []goldenRoute `json:"routes"`
	Checks   []goldenCheck `json:"checks"`
}

type goldenFile struct {
	Scenarios []goldenScenario `json:"scenarios"`
}

func loadGolden(t *testing.T) goldenFile {
	t.Helper()
	raw, err := os.ReadFile("../../testdata/golden/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var gf goldenFile
	if err := json.Unmarshal(raw, &gf); err != nil {
		t.Fatal(err)
	}
	if len(gf.Scenarios) == 0 {
		t.Fatal("golden file has no scenarios")
	}
	return gf
}

func toModelRoutes(t *testing.T, grs []goldenRoute) []netmodel.Route {
	t.Helper()
	out := make([]netmodel.Route, 0, len(grs))
	for _, g := range grs {
		rt, err := decodeRoute(g.ID, g.Prefix, g.AdminDistance, g.Metric, g.Protocol,
			g.Nexthop.Kind, g.Nexthop.Address, g.Nexthop.Interface)
		if err != nil {
			t.Fatalf("golden route %s invalid: %v", g.ID, err)
		}
		out = append(out, rt)
	}
	return out
}

// TestGoldenCasesAgainstCore 让被测核心逐场景逐检查点匹配手写答案，
// 并用独立参考解析器独立计算一遍：核心、参考、手写答案必须三方一致。
func TestGoldenCasesAgainstCore(t *testing.T) {
	gf := loadGolden(t)
	for _, sc := range gf.Scenarios {
		t.Run(sc.Name, func(t *testing.T) {
			routes := toModelRoutes(t, sc.Routes)
			r := rib.New().WithMaxDepth(sc.MaxDepth)
			for _, rt := range routes {
				if err := r.Upsert(rt); err != nil {
					t.Fatalf("upsert %s: %v", rt.ID, err)
				}
			}
			ref := newReferenceResolver(routes, sc.MaxDepth)

			for _, ck := range sc.Checks {
				t.Run(ck.Name, func(t *testing.T) {
					target := parseTarget(t, ck.Target)

					got := r.Lookup(target)
					want := referenceResult{
						status: ck.Status, egress: ck.Egress, depth: ck.Depth,
						chainIDs:      ck.ChainIDs,
						chainPrefixes: ck.ChainPrefixes,
					}
					// 1) 被测核心必须精确匹配手写 golden。
					assertResult(t, "core vs golden", got, want)

					// 2) 独立参考实现也必须推出同一个手写 golden。
					refRes := ref.resolve(target)
					assertRefResult(t, "reference vs golden", refRes, want)

					// 3) 失败类别必须属于受支持的稳定枚举，且诊断非空。
					switch got.Status {
					case rib.StatusForwarded, rib.StatusBlackhole, rib.StatusUnreachable,
						rib.StatusNoRoute, rib.StatusNexthopUnresolved,
						rib.StatusRecursionLoop, rib.StatusDepthExceeded:
					default:
						t.Fatalf("unmapped status %q", got.Status)
					}
					if len(got.Diag) == 0 {
						t.Fatalf("%s: result must explain accept/reject/undecidable", ck.Name)
					}
				})
			}
		})
	}
}

func assertResult(t *testing.T, tag string, got rib.LookupResult, want referenceResult) {
	t.Helper()
	if string(got.Status) != want.status {
		t.Fatalf("%s: status=%s want %s", tag, got.Status, want.status)
	}
	if got.Egress != want.egress {
		t.Fatalf("%s: egress=%q want %q", tag, got.Egress, want.egress)
	}
	if got.Depth != want.depth {
		t.Fatalf("%s: depth=%d want %d; chain=%v", tag, got.Depth, want.depth, got.Chain)
	}
	ids := make([]string, len(got.Chain))
	pfxs := make([]string, len(got.Chain))
	for i, h := range got.Chain {
		ids[i] = h.Chosen.ID
		pfxs[i] = h.Prefix
	}
	if !equalSlice(ids, want.chainIDs) {
		t.Fatalf("%s: chain_ids=%v want %v", tag, ids, want.chainIDs)
	}
	if !equalSlice(pfxs, want.chainPrefixes) {
		t.Fatalf("%s: chain_prefixes=%v want %v", tag, pfxs, want.chainPrefixes)
	}
}

func equalSlice(a, b []string) bool {
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
