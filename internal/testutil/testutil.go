// Package testutil 提供跨包测试共用的合成夹具驱动：虚拟时钟、喂片器、
// 全排列生成，以及被测核心 reasm 与独立 oracle 的交叉校验。
package testutil

import (
	"context"
	"fmt"
	"net/netip"
	"sort"
	"testing"
	"time"

	"ipfragreasm/internal/fixture"
	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/oracle"
	"ipfragreasm/internal/reasm"
	"ipfragreasm/internal/store"
)

// FakeClock 是可手动推进的时钟。
type FakeClock struct{ T time.Time }

// NewFakeClock 从固定起点开始，保证测试可复现。
func NewFakeClock() *FakeClock { return &FakeClock{T: time.Unix(1_700_000_000, 0)} }

// Now 返回当前虚拟时间。
func (c *FakeClock) Now() time.Time { return c.T }

// Advance 推进虚拟时间。
func (c *FakeClock) Advance(d time.Duration) time.Time {
	c.T = c.T.Add(d)
	return c.T
}

// StdHeader 是测试统一使用的分组头（UDP, id 可在调用处覆盖）。
func StdHeader(id uint16) fixture.IPHeaderOptions {
	return fixture.IPHeaderOptions{
		Src:      mustAddr("10.10.0.1"),
		Dst:      mustAddr("10.10.0.2"),
		Protocol: netmodel.ProtoUDP,
		ID:       id,
	}
}

// StdKey 与 StdHeader 对应的分组键。
func StdKey(id uint16) netmodel.FragKey {
	h := StdHeader(id)
	return netmodel.FragKey{Src: h.Src, Dst: h.Dst, Protocol: h.Protocol, ID: h.ID}
}

func mustAddr(s string) netip.Addr {
	a, err := netip.ParseAddr(s)
	if err != nil {
		panic("testutil: 非法地址 " + s + ": " + err.Error())
	}
	return a
}

// SpecsFromSizes 用确定性 pattern 载荷切出一组标准分片（末片可非对齐）。
func SpecsFromSizes(data []byte, sizes []int) []fixture.FragmentSpec {
	return fixture.SplitPayload(data, sizes)
}

// ToOracleSpecs 把合成分片规格转换为 oracle 输入（不共享类型，保持独立）。
func ToOracleSpecs(specs []fixture.FragmentSpec) []oracle.Frag {
	out := make([]oracle.Frag, len(specs))
	for i, s := range specs {
		out[i] = oracle.Frag{Offset: int(s.Offset8) * 8, Payload: s.Payload, More: s.More}
	}
	return out
}

// BuildPackets 把规格编译成“已解析”的 netmodel.Packet（模拟离线抓包解析后的事实）。
func BuildPackets(t *testing.T, h fixture.IPHeaderOptions, specs []fixture.FragmentSpec,
	mutate func(idx int, base fixture.IPHeaderOptions) fixture.IPHeaderOptions) []*netmodel.Packet {
	t.Helper()
	raw := fixture.BuildFragments(h, specs, mutate)
	pkts := make([]*netmodel.Packet, len(raw))
	for i, b := range raw {
		p, err := netmodel.ParseIPv4(b)
		if err != nil {
			t.Fatalf("夹具第 %d 片自身非法: %v", i, err)
		}
		pkts[i] = p
	}
	return pkts
}

// FeedSummary 记录一次喂片序列对被测核心的全部可观察事实。
type FeedSummary struct {
	States     []string
	Kinds      []string // 每步若返回 *Error，记录其 ErrorKind
	Duplicates int
	Accepted   int
	FinalErr   *reasm.Error
	Assembled  []byte
}

// FeedPackets 按给定顺序向重组器喂入已解析分片，返回完整摘要。
// 任一步出现组级终结错误即停止（与 oracle 在首个拒绝处返回的行为对齐）。
func FeedPackets(t *testing.T, asm *reasm.Assembler, pkts []*netmodel.Packet,
	order []int, at func(step int) time.Time) FeedSummary {
	t.Helper()
	ctx := context.Background()
	sum := FeedSummary{}
	for step, idx := range order {
		pkt := pkts[idx]
		var now time.Time
		if at != nil {
			now = at(step)
		}
		var out *reasm.Outcome
		var err error
		if now.IsZero() {
			out, err = asm.Process(ctx, pkt)
		} else {
			out, err = asm.ProcessAt(ctx, pkt, now)
		}
		if err != nil {
			if e, ok := reasm.AsError(err); ok {
				sum.Kinds = append(sum.Kinds, string(e.Kind))
				sum.FinalErr = e
			} else {
				sum.Kinds = append(sum.Kinds, "non-reasm-error:"+err.Error())
			}
			break
		}
		sum.States = append(sum.States, string(out.State))
		if out.Duplicate {
			sum.Duplicates++
		}
		if out.Accepted {
			sum.Accepted++
		}
		if out.State == reasm.StateComplete {
			sum.Assembled = append([]byte(nil), out.Assembled...)
		}
	}
	return sum
}

// Permutations 返回 n 个下标的全排列（n<=6，测试规模受控）。
func Permutations(n int) [][]int {
	if n > 7 {
		panic(fmt.Sprintf("Permutations: n=%d 过大，测试只允许 <=7", n))
	}
	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	var out [][]int
	var heap func(k int)
	heap = func(k int) {
		if k == 1 {
			cp := append([]int(nil), idx...)
			out = append(out, cp)
			return
		}
		heap(k - 1)
		for i := 0; i < k-1; i++ {
			if k%2 == 0 {
				idx[i], idx[k-1] = idx[k-1], idx[i]
			} else {
				idx[0], idx[k-1] = idx[k-1], idx[0]
			}
			heap(k - 1)
		}
	}
	heap(n)
	return out
}

// Interleavings 返回在 n 个正常位置中插入 m 个额外项的全部交错位置选择。
func Interleavings(n, m int) [][]int {
	// 简化版：给出额外项应插入的位置组合（0..n），用于重复片等单额外项场景。
	if m != 1 {
		panic("Interleavings 当前仅支持 m=1")
	}
	var out [][]int
	for pos := 0; pos <= n; pos++ {
		out = append(out, []int{pos})
	}
	return out
}

// OracleCategoryToKind 映射独立 oracle 的拒绝类别到被测核心 ErrorKind。
func OracleCategoryToKind(c oracle.Category) reasm.ErrorKind {
	switch c {
	case oracle.CategoryOverlap:
		return reasm.KindOverlapGroupRejected
	case oracle.CategoryConflictLast:
		return reasm.KindConflictingLastFragment
	case oracle.CategoryTooLarge:
		return reasm.KindDatagramTooLarge
	case oracle.CategoryUnaligned:
		return reasm.KindUnalignedFragment
	default:
		return reasm.ErrorKind("unknown-oracle-category:" + string(c))
	}
}

// AssertOracleAgrees 驱动两套实现并要求结论一致（核心反作弊交叉校验）。
//
// specsPkts: 与 oracleFrags 同序同内容的“已解析分片”；
// order: 喂送顺序（下标）；maxBytes: 重组上限；timedOut: 序列后是否推进到超时。
func AssertOracleAgrees(t *testing.T, runID, caseID string,
	pkts []*netmodel.Packet, oracleFrags []oracle.Frag, order []int,
	maxBytes int, timedOut bool, asm *reasm.Assembler) {
	t.Helper()

	ordered := make([]oracle.Frag, len(order))
	for i, idx := range order {
		ordered[i] = oracleFrags[idx]
	}
	ores := oracle.Reassemble(ordered, maxBytes, timedOut)

	sum := FeedPackets(t, asm, pkts, order, nil)

	switch ores.Verdict {
	case oracle.VerdictComplete:
		if sum.FinalErr != nil {
			t.Fatalf("[%s/%s] oracle 判定 complete，reasm 却拒绝: %s", runID, caseID, sum.FinalErr)
		}
		if len(sum.States) == 0 || sum.States[len(sum.States)-1] != string(reasm.StateComplete) {
			t.Fatalf("[%s/%s] oracle 判定 complete，reasm 末态=%v（未完成）", runID, caseID, sum.States)
		}
		if !bytesEqual(ores.Assembled, sum.Assembled) {
			t.Fatalf("[%s/%s] 重组字节不一致（oracle len=%d, reasm len=%d）",
				runID, caseID, len(ores.Assembled), len(sum.Assembled))
		}
	case oracle.VerdictRejected:
		if sum.FinalErr == nil {
			t.Fatalf("[%s/%s] oracle 判定 rejected(%s)，reasm 却接受并完成: %v",
				runID, caseID, ores.Category, sum.States)
		}
		want := OracleCategoryToKind(ores.Category)
		if sum.FinalErr.Kind != want {
			t.Fatalf("[%s/%s] 拒绝类别不一致: oracle=%s reasm=%s（detail: %s）",
				runID, caseID, want, sum.FinalErr.Kind, sum.FinalErr.Detail)
		}
	case oracle.VerdictPending, oracle.VerdictTimedOut:
		if sum.FinalErr != nil {
			t.Fatalf("[%s/%s] oracle 判定 %s，reasm 却拒绝: %s",
				runID, caseID, ores.Verdict, sum.FinalErr)
		}
		if len(sum.States) > 0 && sum.States[len(sum.States)-1] == string(reasm.StateComplete) {
			t.Fatalf("[%s/%s] oracle 判定 %s（禁止提前输出），reasm 却已完成",
				runID, caseID, ores.Verdict)
		}
	default:
		t.Fatalf("[%s/%s] oracle 出现未知判定 %q", runID, caseID, ores.Verdict)
	}

	if ores.Duplicates != sum.Duplicates {
		t.Fatalf("[%s/%s] 完全重复计数不一致: oracle=%d reasm=%d",
			runID, caseID, ores.Duplicates, sum.Duplicates)
	}
}

// NewAssembler 以内存存储与给定超时构造重组器。
func NewAssembler(t *testing.T, timeout, ttl time.Duration, maxBytes int,
	clk reasm.Clock) (*reasm.Assembler, store.Store) {
	t.Helper()
	st := store.NewMemory()
	asm, err := reasm.New(reasm.Config{
		Timeout: timeout, ResultTTL: ttl, MaxDatagramBytes: maxBytes,
	}, st, clk)
	if err != nil {
		t.Fatalf("New assembler: %v", err)
	}
	return asm, st
}

// SortedKeys 返回组键的稳定排序（日志可读）。
func SortedKeys(keys []netmodel.FragKey) []netmodel.FragKey {
	sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
	return keys
}

func bytesEqual(a, b []byte) bool {
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
