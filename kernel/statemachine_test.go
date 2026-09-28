package kernel

import (
	"errors"
	"testing"
	"time"

	"dsnet/compute"
	"dsnet/proto"
)

func fixedClock() (func() time.Time, *time.Time) {
	t0 := time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)
	cur := t0
	return func() time.Time { cur = cur.Add(time.Second); return cur }, &cur
}

// 驱动辅助：在状态机内找到指定 kind/depth/序号 的任务。
// 计划是确定性的，但 TaskID 随机，因此按深度+派生顺序定位。

func planSpec(t *testing.T, plan compute.Plan) proto.TaskSpec {
	t.Helper()
	if err := compute.ValidatePlan(plan); err != nil {
		t.Fatalf("bad plan: %v", err)
	}
	return proto.TaskSpec{
		TaskID:  proto.TaskID("t_root"),
		Kind:    proto.KindRoot,
		Depth:   0,
		Payload: compute.EncodePlan(plan),
	}
}

func mustClaim(t *testing.T, s *State, id proto.TaskID, w string) (*Decision, proto.LeaseID) {
	t.Helper()
	lease := proto.LeaseID(NewID("l"))
	d, err := s.ApplyClaim(ClaimTask{TaskID: id, LeaseID: lease, WorkerID: proto.WorkerID(w), Lease: time.Minute}, "req")
	if err != nil {
		t.Fatalf("claim %s: %v", id, err)
	}
	return d, lease
}

func mustReport(t *testing.T, s *State, id proto.TaskID, lease proto.LeaseID) *Decision {
	t.Helper()
	d, err := s.ApplyReport(ReportTask{TaskID: id, LeaseID: lease}, "req")
	if err != nil {
		t.Fatalf("report %s: %v", id, err)
	}
	return d
}

func findByDepth(s *State, depth int) []proto.TaskID {
	var out []proto.TaskID
	for _, tk := range s.tasks {
		if tk.spec.Depth == depth {
			out = append(out, tk.spec.TaskID)
		}
	}
	// 按 TaskID 排序保证取法稳定（派生身份唯一）
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j] < out[i] {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}

func getTask(s *State, id proto.TaskID) *task { return s.tasks[id] }

// 场景 1：空队列不是终止；根先报告后才有工作。
func TestEmptyQueueIsNotTermination(t *testing.T) {
	clock, _ := fixedClock()
	s := NewState("j1", planSpec(t, compute.Plan{Fanouts: []int{2}, HashIters: 1}), time.Time{}, clock())
	s.SetClock(clock)

	if s.Phase != proto.PhaseRunning {
		t.Fatalf("初始阶段应为 running，得到 %s", s.Phase)
	}
	// 此刻只有根一个 ready 任务；没有叶子。尝试认领不存在的身份必须失败而非终止。
	if _, err := s.ApplyClaim(ClaimTask{TaskID: "t_nope", LeaseID: "l", WorkerID: "w", Lease: time.Minute}, "req"); err == nil {
		t.Fatal("认领不存在的任务应当失败")
	}
	if s.Phase != proto.PhaseRunning {
		t.Fatalf("空操作后作业不应终态，得到 %s", s.Phase)
	}
	e := s.Evidence()
	if e.RootSettled || e.RootPassive {
		t.Fatalf("根尚未被确认，root_passive/root_settled 必须为 false: %+v", e)
	}
	if e.InFlight != 1 {
		t.Fatalf("在途应为 1（仅根），得到 %d", e.InFlight)
	}
}

// 场景 2：完整运行 plan=[2]（根→2 叶子），验证终止时刻所有计数清偿。
func TestSingleLayerCompletesWithZeroOpenEdges(t *testing.T) {
	clock, _ := fixedClock()
	s := NewState("j1", planSpec(t, compute.Plan{Fanouts: []int{2}, HashIters: 3}), time.Time{}, clock())
	s.SetClock(clock)

	root := s.RootID()
	_, rl := mustClaim(t, s, root, "w0")
	d := mustReport(t, s, root, rl)
	if len(d.Spawned) != 2 {
		t.Fatalf("根应派生 2 个叶子，实际 %d", len(d.Spawned))
	}
	if d.Terminal != nil {
		t.Fatal("根确认时叶子尚未存在/在途，绝不能宣布终止")
	}
	// 根 passive 但 deficit=2，不能结算。
	e := s.Evidence()
	if !e.RootPassive || e.RootDeficit != 2 || e.RootSettled {
		t.Fatalf("根报告后 root(passive=%v deficit=%d settled=%v) 不符合预期",
			e.RootPassive, e.RootDeficit, e.RootSettled)
	}
	if e.InFlight != 2 || e.OpenEdges != 2 {
		t.Fatalf("应剩 2 在途、2 未清偿边，得到 in_flight=%d open=%d", e.InFlight, e.OpenEdges)
	}

	leaves := findByDepth(s, 1)
	if len(leaves) != 2 {
		t.Fatalf("应有 2 个叶子，得到 %d", len(leaves))
	}
	// 乱序：先完成第二个叶子（信号不触发根结算）。
	_, l2lease := mustClaim(t, s, leaves[1], "w2")
	d2 := mustReport(t, s, leaves[1], l2lease)
	if d2.Terminal != nil {
		t.Fatal("仅 1/2 叶子确认时不应终止")
	}
	e = s.Evidence()
	if e.RootDeficit != 1 || e.OpenEdges != 1 || e.InFlight != 1 {
		t.Fatalf("第一片叶子后 deficit/open/inflight 应为 1，得到 %d/%d/%d",
			e.RootDeficit, e.OpenEdges, e.InFlight)
	}

	_, l1lease := mustClaim(t, s, leaves[0], "w1")
	d1 := mustReport(t, s, leaves[0], l1lease)
	if d1.Terminal == nil || *d1.Terminal != proto.PhaseComplete {
		t.Fatalf("最后依赖清偿时必须宣布 complete，得到 %v", d1.Terminal)
	}

	e = s.Evidence()
	// 宣布完成时无未完成任务的硬证据：
	if e.Phase != proto.PhaseComplete {
		t.Fatalf("阶段 %s", e.Phase)
	}
	if e.InFlight != 0 {
		t.Fatalf("完成时在途任务必须为 0，得到 %d", e.InFlight)
	}
	if e.Engaged != 0 {
		t.Fatalf("完成时 engaged 节点必须为 0，得到 %d", e.Engaged)
	}
	if e.Unacknowledged != 0 {
		t.Fatalf("完成时未确认必须为 0，得到 %d", e.Unacknowledged)
	}
	if e.OpenEdges != 0 || e.RootDeficit != 0 {
		t.Fatalf("完成时未清偿因果边必须为 0，得到 open=%d root_deficit=%d",
			e.OpenEdges, e.RootDeficit)
	}
	if e.SpawnedEdges != 2 || e.Signals != 2 {
		t.Fatalf("因果边/信号应各为 2，得到 %d/%d", e.SpawnedEdges, e.Signals)
	}
	if e.SpawnedEdges-e.Signals != 0 {
		t.Fatal("spawned_edges - signals 必须为 0")
	}
	for _, row := range e.ByTask {
		if !row.Passive || !row.Settled || row.Status != proto.StatusReported {
			t.Fatalf("任务 %s 未完全结算: %+v", row.TaskID, row)
		}
	}
	if compute.ExpectedTreeSize(compute.Plan{Fanouts: []int{2}, HashIters: 3}) != 3 {
		t.Fatal("oracle 树规模应为 3")
	}
}

// 场景 3：层级派生 plan=[2,2]（7 任务），中间层与叶子确认顺序打乱。
func TestHierarchicalSpawnOutOfOrderAcks(t *testing.T) {
	clock, _ := fixedClock()
	plan := compute.Plan{Fanouts: []int{2, 2}, HashIters: 2}
	s := NewState("j", planSpec(t, plan), time.Time{}, clock())
	s.SetClock(clock)

	root := s.RootID()
	_, rl := mustClaim(t, s, root, "w0")
	mustReport(t, s, root, rl)
	mids := findByDepth(s, 1)
	if len(mids) != 2 {
		t.Fatalf("第 1 层应为 2，实际 %d", len(mids))
	}

	// 认领两个中间节点，先只报告 mids[1]，产生它的两个叶子。
	_, ml1 := mustClaim(t, s, mids[1], "w1")
	d := mustReport(t, s, mids[1], ml1)
	if len(d.Spawned) != 2 {
		t.Fatalf("中间节点应派生 2 叶子，得到 %d", len(d.Spawned))
	}
	// mids[0] 尚未报告：其叶子尚不存在，系统仍明显未终止。
	if s.Phase != proto.PhaseRunning {
		t.Fatalf("仍有中间任务未确认，阶段不应终态: %s", s.Phase)
	}

	// 收集 mids[1] 的两片叶子并先只确认一片（依赖未清，mid1 不能结算）。
	leaves1 := childrenOf(s, mids[1])
	if len(leaves1) != 2 {
		t.Fatalf("mid1 应有 2 叶子，得到 %d", len(leaves1))
	}
	_, ll := mustClaim(t, s, leaves1[0], "wL")
	ld := mustReport(t, s, leaves1[0], ll)
	if ld.Terminal != nil {
		t.Fatal("叶子只确认一片时不能终止")
	}
	mid1 := getTask(s, mids[1])
	if mid1.deficit != 1 || mid1.settled {
		t.Fatalf("mid1 应 deficit=1 未结算，得到 deficit=%d settled=%v",
			mid1.deficit, mid1.settled)
	}

	// 现在报告 mids[0]，随后把剩余所有叶子以乱序确认。
	_, ml0 := mustClaim(t, s, mids[0], "w0b")
	mustReport(t, s, mids[0], ml0)
	leaves0 := childrenOf(s, mids[0])

	// 先确认 leaves1 的第二片：mid1 此刻才结算（deficit 清零），
	// 但根 deficit 仍有 mids[0] 开放，作业仍 running。
	_, ll2 := mustClaim(t, s, leaves1[1], "wL2")
	d = mustReport(t, s, leaves1[1], ll2)
	if d.Terminal != nil {
		t.Fatal("mid1 结算但 mid0 子树未完成，不能终止")
	}
	if !getTask(s, mids[1]).settled {
		t.Fatal("mid1 应当已结算")
	}
	if e := s.Evidence(); e.RootDeficit != 1 {
		t.Fatalf("根 deficit 此时应为 1（mid0 边开放），得到 %d", e.RootDeficit)
	}

	// 乱序确认 mid0 的两片叶子（先 1 后 0），最后一片触发级联到根。
	_, cl2 := mustClaim(t, s, leaves0[1], "w0c")
	mustReport(t, s, leaves0[1], cl2)
	_, cl1 := mustClaim(t, s, leaves0[0], "w0d")
	final := mustReport(t, s, leaves0[0], cl1)
	if final.Terminal == nil || *final.Terminal != proto.PhaseComplete {
		t.Fatalf("最后一片叶子确认后必须 complete，得到 %v", final.Terminal)
	}

	e := s.Evidence()
	wantTotal := int64(compute.ExpectedTreeSize(plan)) // 7
	if got := int64(len(e.ByTask)); got != wantTotal {
		t.Fatalf("树规模应为 %d，得到 %d", wantTotal, got)
	}
	if e.InFlight != 0 || e.OpenEdges != 0 || e.Engaged != 0 {
		t.Fatalf("完成时必须全部清偿: %+v", e)
	}
	if e.SpawnedEdges != wantTotal-1 || e.Signals != wantTotal-1 {
		t.Fatalf("因果边/信号应各为 %d，得到 %d/%d", wantTotal-1, e.SpawnedEdges, e.Signals)
	}
}

func childrenOf(s *State, parent proto.TaskID) []proto.TaskID {
	var out []proto.TaskID
	for id, tk := range s.tasks {
		if tk.spec.ParentID == parent {
			out = append(out, id)
		}
	}
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j] < out[i] {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}

// 场景 4：重复确认不减少两次计数。
func TestDuplicateAckIsIdempotent(t *testing.T) {
	clock, _ := fixedClock()
	s := NewState("j", planSpec(t, compute.Plan{Fanouts: []int{2}, HashIters: 1}), time.Time{}, clock())
	s.SetClock(clock)
	root := s.RootID()
	_, rl := mustClaim(t, s, root, "w0")
	mustReport(t, s, root, rl)
	leaves := findByDepth(s, 1)
	_, ll := mustClaim(t, s, leaves[0], "w1")
	first := mustReport(t, s, leaves[0], ll)
	signalsAfterFirst := s.Snapshot().Signals
	defAfterFirst := getTask(s, root).deficit

	// 用同一 lease 重复确认。
	dup, err := s.ApplyReport(ReportTask{TaskID: leaves[0], LeaseID: ll}, "req-dup")
	if err != nil {
		t.Fatalf("重复确认应被幂等接受而非报错，得到 %v", err)
	}
	if !dup.Ignored {
		t.Fatal("重复确认必须标记 Ignored")
	}
	// 对另一片已认领的叶子用伪造租约报告：必须拒绝，也绝不动计数。
	_, realLease2 := mustClaim(t, s, leaves[1], "w9")
	_, badErr := s.ApplyReport(ReportTask{TaskID: leaves[1], LeaseID: "l_forged"}, "req-bad")
	var pe *proto.Error
	if !errors.As(badErr, &pe) || pe.Category != proto.FailUnknownLease {
		t.Fatalf("伪造租约类别应为 unknown_lease，得到 %v", badErr)
	}
	if getTask(s, leaves[1]).status != proto.StatusClaimed {
		t.Fatal("被拒报告不得改变任务状态")
	}

	e := s.Evidence()
	if e.DupAcks != 1 {
		t.Fatalf("重复确认计数应为 1，得到 %d", e.DupAcks)
	}
	if e.Signals != signalsAfterFirst {
		t.Fatalf("重复确认不得再次发信号：%d != %d", e.Signals, signalsAfterFirst)
	}
	if getTask(s, root).deficit != defAfterFirst {
		t.Fatal("重复确认不得改变根 deficit")
	}
	if first.Terminal != nil || s.Phase != proto.PhaseRunning {
		t.Fatal("一片叶子未确认时不能终止")
	}

	// 完成另一片叶子，系统仍恰好终止一次（复用其有效租约）。
	final := mustReport(t, s, leaves[1], realLease2)
	if final.Terminal == nil {
		t.Fatal("系统应当终止")
	}
	if e2 := s.Evidence(); e2.Signals != 2 || e2.SpawnedEdges != 2 {
		t.Fatalf("信号/边应各为 2，得到 %d/%d", e2.Signals, e2.SpawnedEdges)
	}
}

// 场景 5：预算超时返回未确认；根永不宣布完成。
func TestBudgetDeadlineReturnsUnacknowledged(t *testing.T) {
	clock, _ := fixedClock()
	s := NewState("j", planSpec(t, compute.Plan{Fanouts: []int{3}, HashIters: 1}), time.Time{}, clock())
	s.SetClock(clock)
	root := s.RootID()
	_, rl := mustClaim(t, s, root, "w0")
	mustReport(t, s, root, rl)
	leaves := findByDepth(s, 1)

	// 只认领并确认一片；第二片只认领导致在途；第三片保持 ready。
	_, done := mustClaim(t, s, leaves[0], "w1")
	mustReport(t, s, leaves[0], done)
	_, stuck := mustClaim(t, s, leaves[1], "w2")
	_ = stuck

	d, err := s.ApplyBudgetDeadline("budget")
	if err != nil {
		t.Fatalf("预算判定: %v", err)
	}
	if d.Terminal == nil || *d.Terminal != proto.PhaseFailed {
		t.Fatalf("预算到期必须 failed，得到 %v", d.Terminal)
	}
	e := s.Evidence()
	if e.Phase != proto.PhaseFailed {
		t.Fatalf("阶段应为 failed，得到 %s", e.Phase)
	}
	if e.Unacknowledged != 2 {
		t.Fatalf("未确认任务应为 2（claimed+ready 各一），得到 %d", e.Unacknowledged)
	}
	if e.RootSettled {
		t.Fatal("预算超时后根绝不允许结算/宣布完成")
	}
	if e.OpenEdges != 2 {
		t.Fatalf("未确认因果边应仍开放 2 条，得到 %d", e.OpenEdges)
	}
	for _, id := range []proto.TaskID{leaves[1], leaves[2]} {
		if getTask(s, id).status != proto.StatusTimedOut {
			t.Fatalf("任务 %s 应为 timed_out", id)
		}
	}

	// 预算后迟到的报告：不能翻案，不能改变计数。
	late, err := s.ApplyReport(ReportTask{TaskID: leaves[1], LeaseID: stuck}, "late")
	if err != nil {
		t.Fatalf("终态后报告应被忽略而非报错，得到 %v", err)
	}
	if !late.Ignored {
		t.Fatal("终态后报告必须 Ignored")
	}
	if e2 := s.Evidence(); e2.Phase != proto.PhaseFailed || e2.OpenEdges != 2 {
		t.Fatalf("迟到报告不得改变结论: %+v", e2)
	}
}

// 场景 6：重复的超时重派不改变在途计数；终态后重派被忽略。
func TestRequeueKeepsInflightCount(t *testing.T) {
	clock, _ := fixedClock()
	s := NewState("j", planSpec(t, compute.Plan{Fanouts: []int{1}, HashIters: 1}), time.Time{}, clock())
	s.SetClock(clock)
	root := s.RootID()
	_, rl := mustClaim(t, s, root, "w0")
	mustReport(t, s, root, rl)
	leaf := findByDepth(s, 1)[0]
	_, lease := mustClaim(t, s, leaf, "w1")
	before := s.Evidence().InFlight // 1
	d, err := s.ApplyRequeue(RequeueExpired{TaskID: leaf}, "rq")
	if err != nil || len(d.Events) != 1 {
		t.Fatalf("重派应产生 1 事件，得到 %v/%d", err, len(d.Events))
	}
	if getTask(s, leaf).status != proto.StatusReady {
		t.Fatal("重派后应回到 ready")
	}
	if s.Evidence().InFlight != before {
		t.Fatalf("重派只是在途身份转移，在途数应不变：%d != %d", s.Evidence().InFlight, before)
	}
	// 重复重派（已 ready）必须忽略。
	d2, _ := s.ApplyRequeue(RequeueExpired{TaskID: leaf}, "rq2")
	if !d2.Ignored {
		t.Fatal("对 ready 任务重派应忽略")
	}
	// 旧租约报告必须被拒（在途转移后旧身份失效）。
	if _, err := s.ApplyReport(ReportTask{TaskID: leaf, LeaseID: lease}, "stale"); err == nil {
		t.Fatal("重派后旧租约报告必须失败")
	}

	// 终态后重派被忽略：先让预算到期，再请求重派。
	if _, err := s.ApplyBudgetDeadline("budget"); err != nil {
		t.Fatalf("预算判定: %v", err)
	}
	d3, err := s.ApplyRequeue(RequeueExpired{TaskID: leaf}, "rq3")
	if err != nil || !d3.Ignored {
		t.Fatalf("终态后重派必须忽略，得到 %+v/%v", d3, err)
	}
}
