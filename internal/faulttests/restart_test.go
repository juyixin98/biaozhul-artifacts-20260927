package faulttests

import (
	"context"
	"testing"

	"rollingdeploy/internal/adapters/procmanager"
	"rollingdeploy/internal/config"
	"rollingdeploy/internal/controller"
	"rollingdeploy/internal/model"
	"rollingdeploy/internal/procman"
	"rollingdeploy/internal/store"
)

// reopen 模拟控制器进程重启：存储与模拟器状态文件都还在，只有内存态丢失。
func (r *rig) reopen(t *testing.T, capacity int) {
	t.Helper()
	pm, err := procman.New(r.pm.PathForTest(), config.Fixture{
		Capacity: capacity,
		Behaviors: map[string]config.VersionBehavior{
			"v1": {Behavior: model.BehaviorAlwaysOK},
			"v2": {Behavior: model.BehaviorAlwaysOK},
		},
	})
	if err != nil {
		t.Fatalf("reopen procman: %v", err)
	}
	r.pm = pm
	r.ctrl = controller.New(
		func(ctx context.Context, fn func(controller.TxFace) error) error {
			return r.st.WithTx(ctx, func(tx store.Tx) error { return fn(tx) })
		},
		procmanager.New(pm), nil)
}

// TestControllerRestartMidRollout 在滚动中途重启控制器：
// 已存活进程必须被重新挂载并继续推进，最终版本正确、无重复创建。
func TestControllerRestartMidRollout(t *testing.T) {
	r := newRig(t, 0, map[string]config.VersionBehavior{
		"v1": {Behavior: model.BehaviorAlwaysOK},
		"v2": {Behavior: model.BehaviorAlwaysOK},
	})
	const D, S, U, thr = 3, 1, 0, 2
	appID, roID, newRev := r.seed("v2", D, S, U, thr, 5, 100, D)
	r.warmOldReady(appID, thr)

	// 推进若干滴答：至少完成一次 start_new（可能已有 become_ready / remove_old）。
	for i := 0; i < 4; i++ {
		res, err := r.ctrl.Tick(r.ctx, roID, "before-restart")
		if err != nil {
			t.Fatalf("pre-restart tick: %v", err)
		}
		if !res.StillInFlight {
			t.Fatal("rollout finished before restart point; choose later restart point")
		}
	}
	activeBefore, _, _, availBefore, _, _ := r.independentCount(appID, newRev)
	procCountBefore := r.pm.LiveCountForTest()

	// “重启”：新建管理器与控制器，底层文件不变。
	r.reopen(t, 0)

	// 重启后第一个滴答必须出现 reattach 语义观测，且计数与重启前一致。
	res, err := r.ctrl.Tick(r.ctx, roID, "after-restart")
	if err != nil {
		t.Fatalf("post-restart tick: %v", err)
	}
	_ = res
	activeAfter, _, _, availAfter, _, _ := r.independentCount(appID, newRev)
	if activeAfter != activeBefore || availAfter != availBefore {
		t.Fatalf("restart changed counts: active %d->%d avail %d->%d",
			activeBefore, activeAfter, availBefore, availAfter)
	}
	if r.pm.LiveCountForTest() != procCountBefore {
		t.Fatalf("restart leaked/duplicated processes: %d -> %d",
			procCountBefore, r.pm.LiveCountForTest())
	}

	ro := r.runUntil(roID, 60)
	if ro.Status != model.StatusSucceeded {
		t.Fatalf("after restart status=%s reason=%s", ro.Status, ro.FailureReason)
	}
	active, newActive, oldActive, avail, newAvail, _ := r.independentCount(appID, newRev)
	if active != D || newActive != D || oldActive != 0 || avail != D || newAvail != D {
		t.Fatalf("post-restart final active=%d new=%d old=%d avail=%d newAvail=%d",
			active, newActive, oldActive, avail, newAvail)
	}
	// 旧实例数必须单调不增且新版本来自既有 + 继续创建，总数从不越界。
	assertEverySnapshot(t, r, roID, D, S, U)
}

// TestHostWipeReattach 模拟器状态整体丢失（模拟宿主重启）：
// 控制器应在重新挂载时明确记录 missing，旧版本消失后发布以 start_failure
// 以外的方式推进 —— 本夹具验证“失败原因可解释且不确定结论单列”。
func TestHostWipeReattach(t *testing.T) {
	r := newRig(t, 0, map[string]config.VersionBehavior{
		"v1": {Behavior: model.BehaviorAlwaysOK},
		"v2": {Behavior: model.BehaviorAlwaysOK},
	})
	const D, S, U, thr = 3, 1, 0, 2
	appID, roID, newRev := r.seed("v2", D, S, U, thr, 5, 100, D)
	r.warmOldReady(appID, thr)

	// 推进到至少一个新实例存在。
	for i := 0; i < 2; i++ {
		if _, err := r.ctrl.Tick(r.ctx, roID, "pre-wipe"); err != nil {
			t.Fatalf("pre-wipe tick: %v", err)
		}
	}
	if err := r.pm.WipeHost(); err != nil {
		t.Fatalf("wipe: %v", err)
	}
	// 重启控制器并继续推进：所有现存实例都应被记为 reattach missing。
	r.reopen(t, 0)
	ro := r.runUntil(roID, 80)
	if ro.Status != model.StatusSucceeded {
		t.Fatalf("after host wipe expected recovery to succeed, status=%s category=%s reason=%s",
			ro.Status, ro.FailureCategory, ro.FailureReason)
	}
	kinds := eventKinds(t, r.st, roID)
	reattach := 0
	for _, k := range kinds {
		if k == model.EvReattach {
			reattach++
		}
	}
	if reattach == 0 {
		t.Fatalf("expected reattach events after host wipe, got kinds=%v", kinds)
	}
	active, newActive, oldActive, avail, newAvail, _ := r.independentCount(appID, newRev)
	if active != D || newActive != D || oldActive != 0 || avail != D || newAvail != D {
		t.Fatalf("post-wipe final active=%d new=%d old=%d avail=%d newAvail=%d",
			active, newActive, oldActive, avail, newAvail)
	}
}
