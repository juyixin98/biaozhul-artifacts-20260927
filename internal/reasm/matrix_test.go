package reasm_test

import (
	"context"
	"fmt"
	"testing"
	"time"

	"ipfragreasm/internal/fixture"
	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/oracle"
	"ipfragreasm/internal/reasm"
	"ipfragreasm/internal/testlog"
	"ipfragreasm/internal/testutil"
)

// TestPermutationWithDuplicateMatrix：遍历 4 片全排列，并在每个可插入位置
// 注入一片“完全重复片”，要求每种组合都重组成功、重复计数精确、字节正确，
// 且结论与独立 oracle 一致。
func TestPermutationWithDuplicateMatrix(t *testing.T) {
	log := testlog.New(t, "reasm/perm-dup-matrix")
	data := patternData(60)
	specs := makeSpecs(data)
	hdr := testutil.StdHeader(0x1001)
	basePkts := testutil.BuildPackets(t, hdr, specs, nil)
	ofrags := testutil.ToOracleSpecs(specs)

	caseNo := 0
	for _, perm := range testutil.Permutations(len(specs)) {
		for dupIdx := 0; dupIdx < len(specs); dupIdx++ {
			// 重复片必须在“其原片已送达之后、且组完成之前”出现，才构成
			// 合法的完全重复语义；否则属于“末片重复/过早复用”等另一类场景，
			// 由 TestExactDuplicateRecognized / TestTimeoutAndIDReuse 专门覆盖。
			origPos := indexOf(perm, dupIdx)
			for at := origPos + 1; at < len(perm); at++ {
				caseNo++
				caseID := fmt.Sprintf("perm=%v/dup=%d/at=%d", perm, dupIdx, at)

				// 构造“包与 oracle 规格严格一一对应”的序列：
				// 在 perm 的 at 位置插入一个完全重复片。
				pktList := make([]*netmodel.Packet, 0, len(perm)+1)
				specsForOracle := make([]oracle.Frag, 0, len(perm)+1)
				for k, idx := range perm {
					if k == at {
						pktList = append(pktList, basePkts[dupIdx])
						specsForOracle = append(specsForOracle, ofrags[dupIdx])
					}
					pktList = append(pktList, basePkts[idx])
					specsForOracle = append(specsForOracle, ofrags[idx])
				}
				send := make([]int, len(pktList))
				for i := range send {
					send[i] = i
				}

				asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
				testutil.AssertOracleAgrees(t, log.RunID(), caseID,
					pktList, specsForOracle, send, 65535, false, asm)

				// 再独立断言终态与重组字节。
				snap, err := asm.Lookup(context.Background(), testutil.StdKey(0x1001))
				if err != nil || snap.State != reasm.StateComplete || !bytesEqualBytes(snap.Assembled, data) {
					t.Fatalf("%s: 终态/字节错误: snap=%v err=%v", caseID, snap, err)
				}
			}
		}
	}
	log.Pass("perm-dup-matrix", fmt.Sprintf("%d-cases", caseNo),
		fmt.Sprintf("全排列×完成前重复片注入 %d 个组合全部与 oracle 一致且字节正确", caseNo),
		map[string]any{"cases": caseNo})
}

// TestOverlapInjectionMatrix：对每种注入位置构造“部分重叠”的坏片，
// 要求一律整组拒绝（overlap_group_rejected），且资源进入终结留存而非提前输出。
func TestOverlapInjectionMatrix(t *testing.T) {
	log := testlog.New(t, "reasm/overlap-matrix")
	data := patternData(60)
	specs := makeSpecs(data)
	hdr := testutil.StdHeader(0x1002)
	good := testutil.BuildPackets(t, hdr, specs, nil)

	// 注入的重叠起点（以 8 字节为单位），特意选“跨合法块边界”的位置，
	// 使坏片 [s, s+16) 与两个相邻块部分相交，且绝不恰好等于某个已接受块
	// （否则会落入“完全重复”类别）。合法块边界（8 单位）: 0,1,3,5。
	overlapStarts8 := []int{0, 2, 4} // 字节 [0,16) [16,32) [32,48)
	// 先计算每个合法块的字节区间。
	blockRanges := make([][2]int, len(specs))
	pos := 0
	for i, sp := range specs {
		blockRanges[i] = [2]int{pos, pos + len(sp.Payload)}
		pos += len(sp.Payload)
	}
	caseNo := 0
	for _, start8 := range overlapStarts8 {
		caseNo++
		caseID := fmt.Sprintf("overlap-start=%d", start8*8)
		sByte, eByte := start8*8, start8*8+16
		badPayload := append([]byte(nil), data[sByte:eByte]...)
		badPayload[0] ^= 0xFF // 再翻转一字节，确保即便区间巧合相同也非完全重复
		badSpec := fixture.FragmentSpec{Offset8: uint16(start8), Payload: badPayload, More: true}
		badPkt := testutil.BuildPackets(t, hdr, []fixture.FragmentSpec{badSpec}, nil)[0]

		// 找到第一个与坏片相交的合法块 k，先送 0..k（保证相交且不触碰末片）。
		k := -1
		for bi, br := range blockRanges {
			if sByte < br[1] && br[0] < eByte {
				k = bi
				break
			}
		}
		if k < 0 || k >= len(specs)-1 {
			t.Fatalf("%s: 用例设计错误：首个相交块 k=%d 非法", caseID, k)
		}

		asm, st := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
		ctx := context.Background()

		for i := 0; i <= k; i++ {
			if _, err := asm.Process(ctx, good[i]); err != nil {
				t.Fatalf("%s: 合法片%d: %v", caseID, i, err)
			}
		}
		_, err := asm.Process(ctx, badPkt)
		e, ok := reasm.AsError(err)
		if !ok || e.Kind != reasm.KindOverlapGroupRejected {
			t.Fatalf("%s: 应 overlap_group_rejected，实际 %v", caseID, err)
		}
		// 资源语义：活动 pending=0；组进入终结留存（store 仍有 1 行审计）。
		stats, _ := asm.Stats(ctx)
		if stats.ActivePending != 0 {
			t.Fatalf("%s: 拒绝后不应残留活动组，实际 %d", caseID, stats.ActivePending)
		}
		if n, _ := st.CountFragments(ctx); n != 0 {
			t.Fatalf("%s: 拒绝后活动分片应清空，实际 %d 行", caseID, n)
		}
		snap, lerr := asm.Lookup(ctx, testutil.StdKey(0x1002))
		if lerr != nil || snap.State != reasm.StateRejected || len(snap.Assembled) != 0 {
			t.Fatalf("%s: 拒绝组应可审计查询且无字节: snap=%v err=%v", caseID, snap, lerr)
		}
		log.Pass("overlap-matrix", caseID, "部分重叠整组拒绝，活动资源清空，终结留存可审计",
			map[string]any{"start_byte": start8 * 8})
	}
}

// TestConflictLastInjectionMatrix：在合法序列的不同注入点插入“伪末片”，
// 要求判定为 conflicting_last_fragment（伪末片位于合法覆盖之后，避免与重叠相交）。
func TestConflictLastInjectionMatrix(t *testing.T) {
	log := testlog.New(t, "reasm/conflict-last-matrix")
	data := patternData(60)
	specs := makeSpecs(data) // 合法覆盖 [0,60)
	hdr := testutil.StdHeader(0x1003)
	good := testutil.BuildPackets(t, hdr, specs, nil)

	// 伪末片落在 60 之后：[64,66)，宣告总长 66。
	fakeSpec := fixture.FragmentSpec{Offset8: 8, Payload: []byte{0xAB, 0xCD}, More: false}
	fakePkt := testutil.BuildPackets(t, hdr, []fixture.FragmentSpec{fakeSpec}, nil)[0]

	for injectAt := 0; injectAt <= 2; injectAt++ {
		caseID := fmt.Sprintf("inject-after=%d", injectAt)
		asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, testutil.NewFakeClock())
		ctx := context.Background()

		// 先送伪末片（建立 total=66），再送 injectAt 个合法片，最后送真实末片。
		if _, err := asm.Process(ctx, fakePkt); err != nil {
			t.Fatalf("%s: 伪末片应被接受: %v", caseID, err)
		}
		for i := 0; i < injectAt; i++ {
			if _, err := asm.Process(ctx, good[i]); err != nil {
				t.Fatalf("%s: 合法片%d: %v", caseID, i, err)
			}
		}
		_, err := asm.Process(ctx, good[len(good)-1]) // 真末片 end=60 != 66
		e, ok := reasm.AsError(err)
		if !ok || e.Kind != reasm.KindConflictingLastFragment {
			t.Fatalf("%s: 应 conflicting_last_fragment，实际 %v", caseID, err)
		}
		log.Pass("conflict-last-matrix", caseID,
			"伪末片宣告 66，真末片终点 60，注入点不同均判冲突末片",
			map[string]any{"injected_after": injectAt})
	}
}

// TestTimeoutReuseAcrossPermutations：每个排列下先扣留一片使组超时，
// 推进时间回收后，再用原排列补齐全部片——要求第二轮重组成功且字节正确。
func TestTimeoutReuseAcrossPermutations(t *testing.T) {
	log := testlog.New(t, "reasm/timeout-perm")
	data := patternData(48)
	specs := fixture.SplitPayload(data, []int{16, 16, 16})
	hdr := testutil.StdHeader(0x1004)
	pkts := testutil.BuildPackets(t, hdr, specs, nil)
	key := testutil.StdKey(0x1004)

	caseNo := 0
	for _, perm := range testutil.Permutations(len(specs)) {
		caseNo++
		caseID := fmt.Sprintf("perm=%v", perm)
		clk := testutil.NewFakeClock()
		asm, st := testutil.NewAssembler(t, 200*time.Millisecond, time.Hour, 65535, clk)
		ctx := context.Background()

		// 第一轮：扣留排列中的最后一个下标，只送前两片。
		for step, idx := range perm[:len(perm)-1] {
			if _, err := asm.ProcessAt(ctx, pkts[idx], clk.Now().Add(time.Duration(step)*time.Millisecond)); err != nil {
				t.Fatalf("%s: 第一轮片: %v", caseID, err)
			}
		}
		clk.Advance(300 * time.Millisecond)
		res, err := asm.SweepAt(ctx, clk.Now())
		if err != nil || len(res.TimedOut) != 1 {
			t.Fatalf("%s: 应恰好超时 1 组: res=%+v err=%v", caseID, res, err)
		}
		if n, _ := st.CountFragments(ctx); n != 0 {
			t.Fatalf("%s: 超时后分片必须回收，剩 %d", caseID, n)
		}

		// 第二轮：原排列全部三片完整送达。
		clk.Advance(time.Second)
		var finalState reasm.State
		for step, idx := range perm {
			out, perr := asm.ProcessAt(ctx, pkts[idx], clk.Now().Add(time.Duration(step)*time.Millisecond))
			if perr != nil {
				t.Fatalf("%s: 第二轮片 %d: %v", caseID, idx, perr)
			}
			finalState = out.State
		}
		if finalState != reasm.StateComplete {
			t.Fatalf("%s: 超时复用后应完成，实际 %s", caseID, finalState)
		}
		snap, _ := asm.Lookup(ctx, key)
		if snap.State != reasm.StateComplete || !bytesEqualBytes(snap.Assembled, data) {
			t.Fatalf("%s: 复用后字节错误: state=%s", caseID, snap.State)
		}
	}
	log.Pass("timeout-perm", fmt.Sprintf("%d-cases", caseNo),
		fmt.Sprintf("%d 种排列下“缺片超时→同 ID 复用→成功”全部成立", caseNo),
		map[string]any{"cases": caseNo, "timeout": "200ms"})
}

func indexOf(xs []int, v int) int {
	for i, x := range xs {
		if x == v {
			return i
		}
	}
	return -1
}

func bytesEqualBytes(a, b []byte) bool {
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
