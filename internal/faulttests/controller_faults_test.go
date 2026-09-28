package faulttests

import (
	"testing"

	"rollingdeploy/internal/config"
	"rollingdeploy/internal/model"
)

// 逐步语义走查。maxUnavailable 约束的是控制器“主动缩容”的决策，
// 真实故障（崩溃/抖动/宿主丢失）会客观降低可用数，控制器要做的是：
// 不把失败计作可用、不让任何调度动作进一步击穿最小可用线、并尽快恢复。
// 以每个滴答的 tick_begin 为基线，逐事件核对快照与事件语义一致：
//   - 任何步骤 total_active <= D+maxSurge；
//   - remove_old 之后 available >= D-maxUnavailable（主动缩容硬边界）；
//   - start_new 不改变 available（创建成功绝不等于就绪）；
//   - become_ready 恰好使 available+1；故障事件的下降以快照为准同步基线。
func assertEverySnapshot(t *testing.T, r *rig, rolloutID string, desired, surge, unavail int) {
	t.Helper()
	evs, err := r.st.ListEventsByRollout(r.ctx, rolloutID)
	if err != nil {
		t.Fatalf("events: %v", err)
	}
	if len(evs) == 0 {
		t.Fatal("no events recorded")
	}
	maxTotal := desired + surge
	minAvail := desired - unavail
	tracker := -1
	lastTick := 0
	for _, e := range evs {
		if e.Snapshot.TotalActive > maxTotal {
			t.Fatalf("event %s tick %d: total_active=%d exceeds D+surge=%d (note=%q)",
				e.Kind, e.Snapshot.Tick, e.Snapshot.TotalActive, maxTotal, e.Note)
		}
		if e.Snapshot.Tick == 0 {
			continue // rollout_created 等滴答外事件
		}
		if e.Snapshot.Tick != lastTick {
			lastTick = e.Snapshot.Tick
			tracker = -1
		}
		switch e.Kind {
		case model.EvTickBegin:
			tracker = e.Snapshot.Available
		case model.EvBecomeReady:
			if e.Snapshot.Available != tracker+1 {
				t.Fatalf("become_ready tick %d: available=%d want baseline+1 (%d)",
					e.Snapshot.Tick, e.Snapshot.Available, tracker+1)
			}
			tracker = e.Snapshot.Available
		case model.EvStartNew:
			if e.Snapshot.Available != tracker {
				t.Fatalf("start_new tick %d: available %d->%d, create must not count as readiness",
					e.Snapshot.Tick, tracker, e.Snapshot.Available)
			}
			if e.Snapshot.TotalActive != e.Snapshot.NewActive+e.Snapshot.OldActive {
				t.Fatalf("start_new tick %d: active counts inconsistent: %+v", e.Snapshot.Tick, e.Snapshot)
			}
		case model.EvStartRejected:
			if e.Snapshot.Available != tracker {
				t.Fatalf("start_rejected tick %d changed available counts", e.Snapshot.Tick)
			}
		case model.EvRemoveOld:
			if e.Snapshot.Available < minAvail {
				t.Fatalf("remove_old tick %d: available=%d below D-unavail=%d (note=%q)",
					e.Snapshot.Tick, e.Snapshot.Available, minAvail, e.Note)
			}
			tracker = e.Snapshot.Available
		case model.EvStartFailed, model.EvProbeDemoted, model.EvReattach:
			// 客观故障决定新的可用数，基线与快照同步。
			tracker = e.Snapshot.Available
		case model.EvRolloutDone, model.EvRolloutFail:
			tracker = e.Snapshot.Available
		}
	}
}

// TestHappyPath 正常滚动：断言伸缩顺序为“先扩新、就绪后再缩旧”，最终全部新版本。
func TestHappyPath(t *testing.T) {
	r := newRig(t, 0, map[string]config.VersionBehavior{
		"v1": {Behavior: model.BehaviorAlwaysOK},
		"v2": {Behavior: model.BehaviorAlwaysOK},
	})
	const D, S, U, thr = 3, 1, 0, 2
	appID, roID, newRev := r.seed("v2", D, S, U, thr, 5, 100, D)
	r.warmOldReady(appID, thr)

	ro := r.runUntil(roID, 60)
	if ro.Status != model.StatusSucceeded {
		t.Fatalf("status=%s reason=%s", ro.Status, ro.FailureReason)
	}

	// 顺序断言：第一个写动作必须是 start_new，且第一次 remove_old 之前
	// 必须已经出现过 become_ready（不能把创建成功当就绪）。
	kinds := eventKinds(t, r.st, roID)
	firstWrite := ""
	for _, k := range kinds {
		if k == model.EvStartNew || k == model.EvRemoveOld {
			firstWrite = k
			break
		}
	}
	if firstWrite != model.EvStartNew {
		t.Fatalf("first write action = %q, want start_new (scale out new before removing old)", firstWrite)
	}
	firstRemove := -1
	readyBeforeRemove := false
	for i, k := range kinds {
		if k == model.EvBecomeReady {
			if firstRemove == -1 {
				readyBeforeRemove = true
			}
		}
		if k == model.EvRemoveOld && firstRemove == -1 {
			firstRemove = i
		}
	}
	if firstRemove < 0 {
		t.Fatal("expected remove_old events, got none")
	}
	if !readyBeforeRemove {
		t.Fatal("no become_ready before first remove_old: controller treated create as ready")
	}

	assertEverySnapshot(t, r, roID, D, S, U)

	active, newActive, oldActive, avail, newAvail, _ := r.independentCount(appID, newRev)
	if active != D || newActive != D || oldActive != 0 || avail != D || newAvail != D {
		t.Fatalf("final counts active=%d new=%d old=%d avail=%d newAvail=%d; want all %d on new revision",
			active, newActive, oldActive, avail, newAvail, D)
	}
}

// TestStartFailure 新版本启动即失败：必须以 start_failure 终态化，失败实例不算可用。
func TestStartFailure(t *testing.T) {
	r := newRig(t, 0, map[string]config.VersionBehavior{
		"v1": {Behavior: model.BehaviorAlwaysOK},
		"v2": {Behavior: model.BehaviorFailStart},
	})
	const D, S, U, thr, limit = 3, 1, 0, 1, 2
	appID, roID, newRev := r.seed("v2", D, S, U, thr, limit, 100, D)
	r.warmOldReady(appID, thr)

	ro := r.runUntil(roID, 20)
	if ro.Status != model.StatusFailed {
		t.Fatalf("status=%s, want failed", ro.Status)
	}
	if ro.FailureCategory != model.FailStartFailure {
		t.Fatalf("category=%q, want %q", ro.FailureCategory, model.FailStartFailure)
	}
	kinds := eventKinds(t, r.st, roID)
	seen := map[string]bool{}
	for _, k := range kinds {
		seen[k] = true
	}
	if !seen[model.EvStartFailed] || !seen[model.EvRolloutFail] {
		t.Fatalf("missing failure events in %v", kinds)
	}
	if seen[model.EvBecomeReady] {
		t.Fatal("failed-start instance must never become ready")
	}
	_, newActive, _, _, newAvail, _ := r.independentCount(appID, newRev)
	if newAvail != 0 {
		t.Fatalf("new_available=%d, failed instance must not count as available", newAvail)
	}
	if newActive > S {
		t.Fatalf("new active=%d must stay within surge=%d", newActive, S)
	}
	assertEverySnapshot(t, r, roID, D, S, U)
}

// TestCrashAfterReady 新版本就绪后崩溃：曾就绪也必须立即移出可用并计入失败。
func TestCrashAfterReady(t *testing.T) {
	r := newRig(t, 0, map[string]config.VersionBehavior{
		"v1": {Behavior: model.BehaviorAlwaysOK},
		"v2": {Behavior: model.BehaviorCrashAfter, Parameter: 1},
	})
	const D, S, U, thr, limit = 3, 1, 0, 1, 2
	appID, roID, newRev := r.seed("v2", D, S, U, thr, limit, 100, D)
	r.warmOldReady(appID, thr)

	ro := r.runUntil(roID, 30)
	if ro.Status != model.StatusFailed || ro.FailureCategory != model.FailStartFailure {
		t.Fatalf("status=%s category=%s reason=%s", ro.Status, ro.FailureCategory, ro.FailureReason)
	}
	kinds := eventKinds(t, r.st, roID)
	// 至少一个新实例曾经 become_ready，随后出现 start_failed。
	idxReady, idxFail := -1, -1
	for i, k := range kinds {
		if k == model.EvBecomeReady && idxReady == -1 {
			idxReady = i
		}
		if k == model.EvStartFailed {
			idxFail = i
		}
	}
	if idxReady < 0 || idxFail < 0 || idxFail < idxReady {
		t.Fatalf("expected become_ready then start_failed, ready=%d fail=%d kinds=%v",
			idxReady, idxFail, kinds)
	}
	_, _, _, _, newAvail, _ := r.independentCount(appID, newRev)
	if newAvail != 0 {
		t.Fatalf("crashed instances counted as available: newAvail=%d", newAvail)
	}
	assertEverySnapshot(t, r, roID, D, S, U)
}

// TestReadinessJitter 探针抖动：连续成功达到阈值才可用，抖动清零但能恢复并最终成功。
func TestReadinessJitter(t *testing.T) {
	r := newRig(t, 0, map[string]config.VersionBehavior{
		"v1": {Behavior: model.BehaviorAlwaysOK},
		// 每 3 次探针失败一次：连续成功被周期性打断。
		"v2": {Behavior: model.BehaviorFlaky, Parameter: 3},
	})
	const D, S, U, thr = 2, 1, 0, 2
	appID, roID, newRev := r.seed("v2", D, S, U, thr, 9, 200, D)
	r.warmOldReady(appID, thr)

	ro := r.runUntil(roID, 120)
	if ro.Status != model.StatusSucceeded {
		t.Fatalf("status=%s reason=%s", ro.Status, ro.FailureReason)
	}
	kinds := eventKinds(t, r.st, roID)
	hasDemotion := false
	for _, k := range kinds {
		if k == model.EvProbeDemoted {
			hasDemotion = true
		}
	}
	if !hasDemotion {
		t.Fatal("jitter scenario must record at least one ready_demoted event")
	}
	_, newActive, _, avail, newAvail, _ := r.independentCount(appID, newRev)
	if newActive != D || avail != D || newAvail != D {
		t.Fatalf("final new=%d avail=%d newAvail=%d want %d", newActive, avail, newAvail, D)
	}
	assertEverySnapshot(t, r, roID, D, S, U)
}

// TestInsufficientCapacity 容量池恰好只能容纳旧版本：创建持续被拒，
// 且 maxUnavailable=0 禁止回收旧实例，必须以 insufficient_capacity 失败。
func TestInsufficientCapacity(t *testing.T) {
	const D, S, U, thr = 3, 1, 0, 1
	r := newRig(t, D, map[string]config.VersionBehavior{
		"v1": {Behavior: model.BehaviorAlwaysOK},
		"v2": {Behavior: model.BehaviorAlwaysOK}, // 版本本身没问题，纯粹是环境容量
	})
	appID, roID, newRev := r.seed("v2", D, S, U, thr, 9, 8, D)
	r.warmOldReady(appID, thr)

	ro := r.runUntil(roID, 15)
	if ro.Status != model.StatusFailed {
		t.Fatalf("status=%s want failed", ro.Status)
	}
	if ro.FailureCategory != model.FailInsufficientCapacity {
		t.Fatalf("category=%q want insufficient_capacity", ro.FailureCategory)
	}
	kinds := eventKinds(t, r.st, roID)
	rejected := 0
	removed := 0
	for _, k := range kinds {
		if k == model.EvStartRejected {
			rejected++
		}
		if k == model.EvRemoveOld {
			removed++
		}
	}
	if rejected == 0 {
		t.Fatal("expected start_rejected events")
	}
	if removed != 0 {
		t.Fatalf("maxUnavailable=0 must forbid old removal, got %d remove_old", removed)
	}
	active, newActive, oldActive, avail, _, oldAvail := r.independentCount(appID, newRev)
	if active != D || oldActive != D || newActive != 0 || avail != D || oldAvail != D {
		t.Fatalf("capacity-exhausted state active=%d new=%d old=%d avail=%d oldAvail=%d",
			active, newActive, oldActive, avail, oldAvail)
	}
	assertEverySnapshot(t, r, roID, D, S, U)
}

// TestCapacityThenRecover 容量暂时不足后通过回收旧版本恢复：先拒绝、后成功。
func TestCapacityThenRecover(t *testing.T) {
	const D, S, U, thr = 2, 1, 1, 1
	r := newRig(t, D+S-1, map[string]config.VersionBehavior{ // 3：允许一个新进程挤入
		"v1": {Behavior: model.BehaviorAlwaysOK},
		"v2": {Behavior: model.BehaviorAlwaysOK},
	})
	appID, roID, newRev := r.seed("v2", D, S, U, thr, 5, 100, D)
	r.warmOldReady(appID, thr)

	ro := r.runUntil(roID, 60)
	if ro.Status != model.StatusSucceeded {
		t.Fatalf("status=%s reason=%s", ro.Status, ro.FailureReason)
	}
	kinds := eventKinds(t, r.st, roID)
	rej, rem := 0, 0
	gotRejectBeforeRemove := false
	for _, k := range kinds {
		switch k {
		case model.EvStartRejected:
			rej++
		case model.EvRemoveOld:
			if rej > 0 {
				gotRejectBeforeRemove = true
			}
			rem++
		}
	}
	if rej == 0 || rem != D || !gotRejectBeforeRemove {
		t.Fatalf("expected rejection then %d removes, got rejected=%d removed=%d beforeRemove=%v kinds=%v",
			D, rej, rem, gotRejectBeforeRemove, kinds)
	}
	_, newActive, oldActive, avail, newAvail, _ := r.independentCount(appID, newRev)
	if newActive != D || oldActive != 0 || avail != D || newAvail != D {
		t.Fatalf("final new=%d old=%d avail=%d newAvail=%d", newActive, oldActive, avail, newAvail)
	}
	assertEverySnapshot(t, r, roID, D, S, U)
}

// TestInvalidStrategyDeadlock maxSurge=0 且 maxUnavailable=0 更新时零推进。
func TestInvalidStrategyDeadlock(t *testing.T) {
	r := newRig(t, 0, map[string]config.VersionBehavior{
		"v1": {Behavior: model.BehaviorAlwaysOK},
		"v2": {Behavior: model.BehaviorAlwaysOK},
	})
	const D, S, U, thr = 3, 0, 0, 1
	appID, roID, _ := r.seed("v2", D, S, U, thr, 5, 100, D)
	r.warmOldReady(appID, thr)

	ro := r.runUntil(roID, 10)
	if ro.Status != model.StatusFailed || ro.FailureCategory != model.FailInvalidStrategy {
		t.Fatalf("status=%s category=%s reason=%s", ro.Status, ro.FailureCategory, ro.FailureReason)
	}
	kinds := eventKinds(t, r.st, roID)
	for _, k := range kinds {
		if k == model.EvStartNew || k == model.EvRemoveOld {
			t.Fatalf("deadlocked strategy must take no mutation action, saw %s in %v", k, kinds)
		}
	}
}
