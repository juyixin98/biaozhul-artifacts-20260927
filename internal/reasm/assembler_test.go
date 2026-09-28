package reasm_test

import (
	"bytes"
	"context"
	"fmt"
	"net/netip"
	"testing"
	"time"

	"ipfragreasm/internal/fixture"
	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/reasm"
	"ipfragreasm/internal/testlog"
	"ipfragreasm/internal/testutil"
)

// patternData 返回位置可辨识载荷，任何拼接错位都会暴露。
func patternData(n int) []byte { return fixture.PatternPayload(n) }

// makeSpecs 返回标准 4 片（8,16,16,20 -> 总长 60，末片非对齐）。
func makeSpecs(data []byte) []fixture.FragmentSpec {
	return fixture.SplitPayload(data, []int{8, 16, 16, 20})
}

// TestAllPermutationsReassemble：遍历 4 片的 24 种到达排列，
// 包括“末片先到”，要求每种都重组出与 oracle 一致的字节。
func TestAllPermutationsReassemble(t *testing.T) {
	log := testlog.New(t, "reasm/permutations")
	data := patternData(60)
	specs := makeSpecs(data)
	hdr := testutil.StdHeader(0x0101)
	pkts := testutil.BuildPackets(t, hdr, specs, nil)
	ofrags := testutil.ToOracleSpecs(specs)

	for _, perm := range testutil.Permutations(len(specs)) {
		caseID := fmt.Sprintf("perm=%v", perm)
		log.Step("perm", caseID, "按排列喂入 4 片，oracle 与 reasm 交叉校验")
		asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
		testutil.AssertOracleAgrees(t, log.RunID(), caseID, pkts, ofrags, perm, 65535, false, asm)

		// 独立断言：重组字节与原始载荷逐字节相等。
		snap, err := asm.Lookup(context.Background(), testutil.StdKey(0x0101))
		if err != nil {
			t.Fatalf("%s: Lookup: %v", caseID, err)
		}
		if snap.State != reasm.StateComplete {
			t.Fatalf("%s: 终态=%s 期望 complete（即使末片先到也不得提前输出，收齐才完成）",
				caseID, snap.State)
		}
		if !bytes.Equal(snap.Assembled, data) {
			t.Fatalf("%s: 重组字节与载荷不符", caseID)
		}
		log.Pass("perm", caseID,
			fmt.Sprintf("4 片排列 %v 重组成功且字节逐位相等 len=%d", perm, len(data)),
			map[string]any{"permutation": perm, "length": len(data)})
	}
}

// TestLastFragmentFirstDoesNotEmitEarly：末片先到时必须保持 pending，缺片不得输出。
func TestLastFragmentFirstDoesNotEmitEarly(t *testing.T) {
	log := testlog.New(t, "reasm/last-first")
	data := patternData(40)
	specs := fixture.SplitPayload(data, []int{8, 16, 16})
	hdr := testutil.StdHeader(0x0202)
	pkts := testutil.BuildPackets(t, hdr, specs, nil)
	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
	ctx := context.Background()

	// 先送末片（index 2）。
	out, err := asm.Process(ctx, pkts[2])
	if err != nil {
		t.Fatalf("末片先到被拒绝: %v", err)
	}
	if out.State != reasm.StatePending {
		t.Fatalf("末片先到后状态=%s，期望 pending", out.State)
	}
	if len(out.Assembled) != 0 {
		t.Fatalf("缺片时不得提前输出任何重组字节")
	}
	if !out.Progress.HasLast || out.Progress.TotalLength != 40 || out.Progress.Covered != 0 {
		t.Fatalf("进度字段错误: %+v", out.Progress)
	}
	log.Step("last-first", "last-only", "末片先到：HasLast=true, Covered=0, 状态 pending")

	// 再送中间片，仍有缺口。
	if _, err := asm.Process(ctx, pkts[1]); err != nil {
		t.Fatalf("中间片: %v", err)
	}
	snap, _ := asm.Lookup(ctx, testutil.StdKey(0x0202))
	if snap.State != reasm.StatePending || snap.Progress.Covered != 0 {
		t.Fatalf("缺首片时 Covered 必须为 0，实际 %+v", snap.Progress)
	}

	// 最后送首片，收齐才完成。
	out, err = asm.Process(ctx, pkts[0])
	if err != nil {
		t.Fatalf("首片: %v", err)
	}
	if out.State != reasm.StateComplete || !bytes.Equal(out.Assembled, data) {
		t.Fatalf("收齐后未正确完成: state=%s", out.State)
	}
	log.Pass("last-first", "last-only", "末片先到不提前输出，收齐后字节正确",
		map[string]any{"covered_before_first": 0, "length": 40})
}

// TestExactDuplicateRecognized：完全重复片单独识别、幂等且不触发覆盖拒绝。
func TestExactDuplicateRecognized(t *testing.T) {
	log := testlog.New(t, "reasm/duplicate")
	data := patternData(48)
	specs := fixture.SplitPayload(data, []int{16, 16, 16})
	hdr := testutil.StdHeader(0x0303)
	pkts := testutil.BuildPackets(t, hdr, specs, nil)
	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
	ctx := context.Background()

	// 0,1,重复1,2
	out, _ := asm.Process(ctx, pkts[0])
	if !out.Accepted {
		t.Fatalf("首片应 accepted")
	}
	out, _ = asm.Process(ctx, pkts[1])
	if !out.Accepted {
		t.Fatalf("第二片应 accepted")
	}
	out, err := asm.Process(ctx, pkts[1]) // 完全重复
	if err != nil {
		t.Fatalf("完全重复片不应报错: %v", err)
	}
	if !out.Duplicate || out.Accepted {
		t.Fatalf("重复片应 duplicate=true accepted=false，实际 %+v", out)
	}
	if out.Progress.Duplicates != 1 || out.Progress.Received != 2 {
		t.Fatalf("重复计数错误: %+v", out.Progress)
	}
	log.Step("duplicate", "dup-of-1", "完全重复片识别成功，Received 不增长")

	out, _ = asm.Process(ctx, pkts[2])
	if out.State != reasm.StateComplete || !bytes.Equal(out.Assembled, data) {
		t.Fatalf("重复后仍应正常完成")
	}
	log.Pass("duplicate", "full", "完全重复幂等，重组字节与原始载荷一致",
		map[string]any{"duplicates": 1, "received": 3})
}

// TestSameRangeDifferentBytesRejectsGroup：同区间不同字节 -> 整组拒绝。
func TestSameRangeDifferentBytesRejectsGroup(t *testing.T) {
	log := testlog.New(t, "reasm/same-range-diff")
	data := patternData(48)
	specs := fixture.SplitPayload(data, []int{16, 16, 16})
	hdr := testutil.StdHeader(0x0404)
	// 把第二片的载荷首字节改坏，再单独构造一个“同 offset/len/MF 但内容不同”的片。
	badSpec := specs[1]
	badPayload := append([]byte(nil), badSpec.Payload...)
	badPayload[0] ^= 0xFF
	badPkts := testutil.BuildPackets(t, hdr,
		[]fixture.FragmentSpec{badSpec, {Offset8: badSpec.Offset8, Payload: badPayload, More: badSpec.More}}, nil)

	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
	ctx := context.Background()
	good := testutil.BuildPackets(t, hdr, specs, nil)
	if _, err := asm.Process(ctx, good[0]); err != nil {
		t.Fatalf("片0: %v", err)
	}
	if _, err := asm.Process(ctx, good[1]); err != nil {
		t.Fatalf("片1: %v", err)
	}
	_, err := asm.Process(ctx, badPkts[1])
	if e, ok := reasm.AsError(err); !ok || e.Kind != reasm.KindOverlapGroupRejected {
		t.Fatalf("同区间不同字节应 overlap_group_rejected，实际 %v", err)
	}
	// 整组拒绝：再送合法第三片也必须被拒绝（组已终结）。
	_, err = asm.Process(ctx, good[2])
	if e, ok := reasm.AsError(err); !ok || e.Kind != reasm.KindGroupAlreadyTerminal {
		t.Fatalf("整组拒绝后组必须终结，再送片应 already_terminal，实际 %v", err)
	}
	snap, _ := asm.Lookup(ctx, testutil.StdKey(0x0404))
	if snap.State != reasm.StateRejected || snap.Reason != string(reasm.KindOverlapGroupRejected) {
		t.Fatalf("终态应 rejected/overlap，实际 state=%s reason=%s", snap.State, snap.Reason)
	}
	if len(snap.Assembled) != 0 {
		t.Fatalf("拒绝组绝不能输出重组字节")
	}
	log.Pass("same-range-diff", "reject", "同区间不同字节整组拒绝，后续片不再污染已终结组",
		map[string]any{"kind": reasm.KindOverlapGroupRejected})
}

// TestPartialOverlapRejectsGroup：非完全重复的部分字节相交 -> 整组拒绝。
func TestPartialOverlapRejectsGroup(t *testing.T) {
	log := testlog.New(t, "reasm/partial-overlap")
	data := patternData(48)
	specs := fixture.SplitPayload(data, []int{16, 16, 16})
	hdr := testutil.StdHeader(0x0505)
	// 覆盖片：offset=8（与片0[0,16) 和片1[16,32) 都相交），长度16。
	overlap := fixture.FragmentSpec{Offset8: 1, Payload: data[8:24], More: true}
	ovlPkts := testutil.BuildPackets(t, hdr, []fixture.FragmentSpec{overlap}, nil)
	good := testutil.BuildPackets(t, hdr, specs, nil)

	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
	ctx := context.Background()
	_, _ = asm.Process(ctx, good[0])
	_, err := asm.Process(ctx, ovlPkts[0])
	if e, ok := reasm.AsError(err); !ok || e.Kind != reasm.KindOverlapGroupRejected {
		t.Fatalf("部分重叠应整组拒绝，实际 %v", err)
	}
	log.Pass("partial-overlap", "reject", "[8,24) 与 [0,16) 部分相交，整组拒绝",
		map[string]any{"kind": reasm.KindOverlapGroupRejected})
}

// TestConflictingLastFragment：先到的末片宣告不同总长 -> 冲突拒绝。
func TestConflictingLastFragment(t *testing.T) {
	log := testlog.New(t, "reasm/conflict-last")
	data := patternData(40)
	specs := fixture.SplitPayload(data, []int{8, 16, 16}) // 真实末片 end=40
	hdr := testutil.StdHeader(0x0606)
	// 伪末片必须完全位于真实覆盖范围“之后”，避免与真实片相交而落入重叠类别：
	// [40,42) MF=0 宣告总长 42；真实覆盖 [0,40) 与其相邻不重叠，
	// 真实末片终点 40 != 42 才纯粹命中 conflicting_last_fragment。
	fakeLast := fixture.FragmentSpec{Offset8: 5, Payload: []byte{0xAB, 0xCD}, More: false}
	all := append([]fixture.FragmentSpec{fakeLast}, specs...)
	pkts := testutil.BuildPackets(t, hdr, all, nil)
	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
	ctx := context.Background()

	if _, err := asm.Process(ctx, pkts[0]); err != nil {
		t.Fatalf("伪末片应先被接受为 pending: %v", err)
	}
	// 送真实首片与中间片（不越过 34）。
	if _, err := asm.Process(ctx, pkts[1]); err != nil { // 真实片0 [0,8)
		t.Fatalf("真实片0: %v", err)
	}
	// 真实末片 end=40 与已宣告 34 冲突。
	_, err := asm.Process(ctx, pkts[3]) // all[3] = specs[2] 真末片
	if e, ok := reasm.AsError(err); !ok || e.Kind != reasm.KindConflictingLastFragment {
		t.Fatalf("冲突末片应 conflicting_last_fragment，实际 %v", err)
	}
	log.Pass("conflict-last", "reject", "末片终点 40 与已宣告 34 冲突，整组拒绝",
		map[string]any{"kind": reasm.KindConflictingLastFragment})
}

// TestFragmentBeyondDeclaredTotal：非末片越过已知总长 -> 冲突拒绝。
func TestFragmentBeyondDeclaredTotal(t *testing.T) {
	data := patternData(32)
	specs := fixture.SplitPayload(data, []int{16, 16}) // 末片 end=32
	hdr := testutil.StdHeader(0x0707)
	// 先送真实末片 [16,32) 宣告总长 32；越界片必须从 32 之后开始才不与之重叠：
	// [32,48) MF=1，长度对齐 8，end=48 > 32 -> 纯粹命中 conflicting_last_fragment。
	beyond := fixture.FragmentSpec{Offset8: 4, Payload: make([]byte, 16), More: true}
	pkts := testutil.BuildPackets(t, hdr, []fixture.FragmentSpec{specs[1], beyond}, nil)
	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
	ctx := context.Background()
	if _, err := asm.Process(ctx, pkts[0]); err != nil {
		t.Fatalf("末片: %v", err)
	}
	_, err := asm.Process(ctx, pkts[1])
	if e, ok := reasm.AsError(err); !ok || e.Kind != reasm.KindConflictingLastFragment {
		t.Fatalf("越过总长应 conflicting_last_fragment，实际 %v", err)
	}
}

// TestUnalignedNonLastFragment：MF=1 且长度非 8 倍数 -> 单片非法（不污染组）。
func TestUnalignedNonLastFragment(t *testing.T) {
	log := testlog.New(t, "reasm/unaligned")
	hdr := testutil.StdHeader(0x0808)
	// MF=1 片长 7，offset=0，非法。
	bad := fixture.FragmentSpec{Offset8: 0, Payload: make([]byte, 7), More: true}
	pkts := testutil.BuildPackets(t, hdr, []fixture.FragmentSpec{bad}, nil)
	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
	_, err := asm.Process(context.Background(), pkts[0])
	e, ok := reasm.AsError(err)
	if !ok || e.Kind != reasm.KindUnalignedFragment {
		t.Fatalf("应对单片返回 unaligned_fragment，实际 %v", err)
	}
	// 单片非法不建组：同键随后合法送达应可正常重组（不被污染）。
	data := patternData(24)
	goodSpecs := fixture.SplitPayload(data, []int{8, 16})
	good := testutil.BuildPackets(t, hdr, goodSpecs, nil)
	out, err := asm.Process(context.Background(), good[0])
	if err != nil {
		t.Fatalf("非法片不应污染组，合法首片却报错: %v", err)
	}
	out, err = asm.Process(context.Background(), good[1])
	if err != nil || out.State != reasm.StateComplete || !bytes.Equal(out.Assembled, data) {
		t.Fatalf("非法片后合法序列应完成，err=%v state=%s", err, out.State)
	}
	log.Pass("unaligned", "single-fragment", "MF=1 长度 7 为单片非法，不污染同键后续重组",
		map[string]any{"kind": reasm.KindUnalignedFragment})
}

// TestDatagramTooLarge：片终点超过配置上限 -> 整组拒绝（409 类）。
func TestDatagramTooLarge(t *testing.T) {
	hdr := testutil.StdHeader(0x0909)
	// offset8=7997（字节 63976），长度 32（8 对齐，避免落入 unaligned 类别），
	// end=64008 超过上限 64000。
	big := fixture.FragmentSpec{Offset8: 7997, Payload: make([]byte, 32), More: true}
	pkts := testutil.BuildPackets(t, hdr, []fixture.FragmentSpec{big}, nil)
	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 64000, testutil.NewFakeClock())
	_, err := asm.Process(context.Background(), pkts[0])
	if e, ok := reasm.AsError(err); !ok || e.Kind != reasm.KindDatagramTooLarge {
		t.Fatalf("超长应 datagram_too_large，实际 %v", err)
	}
}

// TestTimeoutAndIDReuse：缺片超时彻底回收后，同 ID 完整序列可重组；
// 而完成组在 TTL 内复用同 ID 必须被拒绝。
func TestTimeoutAndIDReuse(t *testing.T) {
	log := testlog.New(t, "reasm/timeout-reuse")
	clk := testutil.NewFakeClock()
	asm, st := testutil.NewAssembler(t, 500*time.Millisecond, time.Hour, 65535, clk)
	ctx := context.Background()
	key := testutil.StdKey(0x0A0A)

	data := patternData(32)
	specs := fixture.SplitPayload(data, []int{16, 16})
	pkts := testutil.BuildPackets(t, testutil.StdHeader(0x0A0A), specs, nil)

	// 第一轮：只送首片，然后超时。
	if _, err := asm.ProcessAt(ctx, pkts[0], clk.Now()); err != nil {
		t.Fatalf("首片: %v", err)
	}
	clk.Advance(600 * time.Millisecond)
	res, err := asm.SweepAt(ctx, clk.Now())
	if err != nil {
		t.Fatalf("Sweep: %v", err)
	}
	if len(res.TimedOut) != 1 || res.TimedOut[0] != key {
		t.Fatalf("应超时回收该键，实际 %+v", res)
	}
	// 超时后资源彻底回收：store 中无组、无分片。
	if n, _ := st.CountFragments(ctx); n != 0 {
		t.Fatalf("超时后分片应彻底回收，剩余 %d 行", n)
	}
	if groups, _ := st.ListAllGroups(ctx); len(groups) != 0 {
		t.Fatalf("超时后组行应删除，剩余 %d", len(groups))
	}
	if _, err := asm.Lookup(ctx, key); err == nil {
		t.Fatalf("超时回收后 Lookup 应 not found")
	}
	log.Step("timeout-reuse", "timed-out", "首片后超时，组与分片彻底回收，键立即可复用")

	// 第二轮：同 ID 完整送达，必须成功。
	clk.Advance(time.Second)
	out, err := asm.ProcessAt(ctx, pkts[0], clk.Now())
	if err != nil || out.State != reasm.StatePending {
		t.Fatalf("超时复用首片应 accepted/pending，err=%v state=%s", err, out.State)
	}
	out, err = asm.ProcessAt(ctx, pkts[1], clk.Now())
	if err != nil || out.State != reasm.StateComplete || !bytes.Equal(out.Assembled, data) {
		t.Fatalf("超时复用后应重组成功，err=%v", err)
	}
	log.Step("timeout-reuse", "reused-complete", "同 ID 复用重组成功")

	// 第三轮：组刚完成且 TTL 很长，立刻复用同 ID 必须被拒绝（过早复用）。
	_, err = asm.ProcessAt(ctx, pkts[0], clk.Now())
	if e, ok := reasm.AsError(err); !ok || e.Kind != reasm.KindGroupAlreadyTerminal {
		t.Fatalf("完成留存期内复用 ID 应 already_terminal，实际 %v", err)
	}
	log.Pass("timeout-reuse", "ttl-blocks-reuse",
		"超时释放 ID 可复用；完成组在 TTL 内阻断过早复用",
		map[string]any{"timeout": "500ms", "ttl": "1h"})
}

// TestResultTTLRecycling：完成组在 ResultTTL 到期后被彻底回收，之后可复用。
func TestResultTTLRecycling(t *testing.T) {
	clk := testutil.NewFakeClock()
	asm, st := testutil.NewAssembler(t, time.Hour, 500*time.Millisecond, 65535, clk)
	ctx := context.Background()
	key := testutil.StdKey(0x0B0B)

	data := patternData(24)
	specs := fixture.SplitPayload(data, []int{8, 16})
	pkts := testutil.BuildPackets(t, testutil.StdHeader(0x0B0B), specs, nil)
	_, _ = asm.ProcessAt(ctx, pkts[0], clk.Now())
	_, _ = asm.ProcessAt(ctx, pkts[1], clk.Now())

	if groups, _ := st.ListAllGroups(ctx); len(groups) != 1 {
		t.Fatalf("完成后应留 1 个组行审计")
	}
	clk.Advance(600 * time.Millisecond)
	res, err := asm.SweepAt(ctx, clk.Now())
	if err != nil {
		t.Fatalf("Sweep: %v", err)
	}
	if len(res.Recycled) != 1 || res.Recycled[0] != key {
		t.Fatalf("TTL 到期应回收，实际 %+v", res)
	}
	if groups, _ := st.ListAllGroups(ctx); len(groups) != 0 {
		t.Fatalf("回收后应无组行")
	}
	// 回收后同 ID 又可使用。
	out, err := asm.ProcessAt(ctx, pkts[0], clk.Now())
	if err != nil || out.State != reasm.StatePending {
		t.Fatalf("TTL 回收后应可重新建组，err=%v state=%s", err, out.State)
	}
}

// TestGroupKeyDistinguishesFlow：src/dst/protocol/id 任一不同即为不同组，
// 显式验证“IP 重组不是 TCP 流重组”（协议号是键，端口/序号完全不参与）。
func TestGroupKeyDistinguishesFlow(t *testing.T) {
	base := testutil.StdHeader(0x0C0C)
	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
	ctx := context.Background()

	keys := []netmodel.FragKey{
		{Src: base.Src, Dst: base.Dst, Protocol: netmodel.ProtoTCP, ID: 1},
		{Src: base.Src, Dst: base.Dst, Protocol: netmodel.ProtoUDP, ID: 1}, // 协议不同
		{Src: base.Src, Dst: base.Dst, Protocol: netmodel.ProtoUDP, ID: 2}, // ID 不同
	}
	// 改地址。
	altDst := base
	altDst.Dst = netip.MustParseAddr("10.10.0.9")
	altDst.ID = 1
	keys = append(keys, netmodel.FragKey{Src: altDst.Src, Dst: altDst.Dst, Protocol: altDst.Protocol, ID: altDst.ID})

	for i, k := range keys {
		spec := fixture.FragmentSpec{Offset8: 0, Payload: make([]byte, 8), More: true}
		h := fixture.IPHeaderOptions{Src: k.Src, Dst: k.Dst, Protocol: k.Protocol, ID: k.ID}
		p := testutil.BuildPackets(t, h, []fixture.FragmentSpec{spec}, nil)
		if _, err := asm.Process(ctx, p[0]); err != nil {
			t.Fatalf("键 %d 首片: %v", i, err)
		}
	}
	stats, _ := asm.Stats(ctx)
	if stats.ActivePending != len(keys) {
		t.Fatalf("4 个不同分组键应产生 4 个独立 pending 组，实际 %d", stats.ActivePending)
	}
}
