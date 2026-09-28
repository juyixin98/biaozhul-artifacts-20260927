package model_test

import (
	"testing"

	"rollingdeploy/internal/model"
)

// 纯函数级独立断言：可用计数规则不依赖控制器实现，
// 期望值在测试里按定义直接列出，防止“核心给自己判卷”。
func TestComputeSnapshotCounts(t *testing.T) {
	const target = "rev-new"
	mk := func(id, rev, phase string) *model.Instance {
		return &model.Instance{ID: id, RevisionID: rev, Phase: phase}
	}
	insts := []*model.Instance{
		mk("1", "rev-old", model.PhaseReady),      // 旧-可用
		mk("2", "rev-old", model.PhaseStarting),   // 旧-探针未满：不可用
		mk("3", "rev-old", model.PhaseFailed),     // 旧-失败：活跃占容量但绝不可用
		mk("4", target, model.PhaseStarting),      // 新-刚创建：占 surge，不可用
		mk("5", target, model.PhaseReady),         // 新-可用
		mk("6", target, model.PhaseFailed),        // 新-失败：活跃占容量但绝不可用
		mk("7", "rev-old", model.PhaseTerminated), // 已终止：纯审计行，不占容量
	}
	s := model.ComputeSnapshot(insts, target, 4, 2, 1, 7)

	if s.TotalActive != 6 {
		t.Fatalf("total_active=%d want 6 (terminated excluded)", s.TotalActive)
	}
	if s.OldActive != 3 || s.NewActive != 3 {
		t.Fatalf("active split old=%d new=%d want 3/3", s.OldActive, s.NewActive)
	}
	if s.Available != 2 || s.OldAvailable != 1 || s.NewAvailable != 1 {
		t.Fatalf("available total=%d old=%d new=%d want 2/1/1 (starting & failed never available)",
			s.Available, s.OldAvailable, s.NewAvailable)
	}
	if s.MaxTotal != 6 || s.MinAvailable != 3 {
		t.Fatalf("bounds max=%d min=%d want 6/3", s.MaxTotal, s.MinAvailable)
	}
	// 失败实例再多，也绝不能被计入可用。
	insts = append(insts, mk("8", target, model.PhaseFailed), mk("9", target, model.PhaseFailed))
	s = model.ComputeSnapshot(insts, target, 4, 2, 1, 8)
	if s.Available != 2 || s.NewAvailable != 1 {
		t.Fatalf("failed instances leaked into availability: %+v", s)
	}
}

// 就绪阈值语义：Ready 阶段只应由协调层在持续达标后置入；
// 模型层严格保证只有 ready 计入可用，与连续计数的存储位置无关。
func TestIsActive(t *testing.T) {
	cases := map[string]bool{
		model.PhaseStarting:   true,
		model.PhaseReady:      true,
		model.PhaseFailed:     true,
		model.PhaseTerminated: false,
	}
	for ph, want := range cases {
		if got := model.IsActive(ph); got != want {
			t.Fatalf("IsActive(%q)=%v want %v", ph, got, want)
		}
	}
}
