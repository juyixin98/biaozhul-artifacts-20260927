// Package faulttests 包含针对协调循环的故障注入单元测试。
//
// 与 blackbox 独立测试的区别：这里直接驱动 controller.Tick，
// 使用真实 SQLite 存储 + 模拟进程管理器，但断言由测试代码自行计数，
// 不调用任何被测函数来“生成期望答案”。
package faulttests

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"rollingdeploy/internal/adapters/procmanager"
	"rollingdeploy/internal/config"
	"rollingdeploy/internal/controller"
	"rollingdeploy/internal/ids"
	"rollingdeploy/internal/model"
	"rollingdeploy/internal/procman"
	"rollingdeploy/internal/store"
)

type rig struct {
	t    *testing.T
	st   *store.Store
	pm   *procman.Manager
	ctrl *controller.Controller
	ctx  context.Context
}

func newRig(t *testing.T, capacity int, behaviors map[string]config.VersionBehavior) *rig {
	t.Helper()
	dir := t.TempDir()
	st, err := store.Open(filepath.Join(dir, "test.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	pm, err := procman.New(filepath.Join(dir, "pm.json"), config.Fixture{
		Capacity: capacity, Behaviors: behaviors,
	})
	if err != nil {
		t.Fatalf("new procman: %v", err)
	}
	ctrl := controller.New(
		func(ctx context.Context, fn func(controller.TxFace) error) error {
			return st.WithTx(ctx, func(tx store.Tx) error { return fn(tx) })
		},
		procmanager.New(pm), nil)
	return &rig{t: t, st: st, pm: pm, ctrl: ctrl, ctx: context.Background()}
}

// seed 创建应用、旧版本/新版本与初始实例，并创建一个在途 rollout。
func (r *rig) seed(version string, replicas, surge, unavail, threshold, failLimit, ticks int,
	initialOld int) (appID, rolloutID, newRevID string) {
	t := r.t
	now := time.Now()
	appID = ids.New("app")
	oldRev := ids.New("rev")
	newRevID = ids.New("rev")
	rolloutID = ids.New("rol")
	if err := r.st.WithTx(r.ctx, func(tx store.Tx) error {
		if err := tx.CreateApp(r.ctx, &model.App{ID: appID, Name: "w" + appID[4:], Replicas: replicas, CreatedAt: now}); err != nil {
			return err
		}
		if err := tx.CreateRevision(r.ctx, &model.Revision{ID: oldRev, AppID: appID, Version: "v1", CreatedAt: now}); err != nil {
			return err
		}
		if err := tx.CreateRevision(r.ctx, &model.Revision{ID: newRevID, AppID: appID, Version: version, CreatedAt: now}); err != nil {
			return err
		}
		ro := &model.Rollout{
			ID: rolloutID, AppID: appID, Op: model.OpRollout, RevisionID: newRevID,
			PrevRevisionID: oldRev, Replicas: replicas, MaxSurge: surge, MaxUnavailable: unavail,
			ReadyThreshold: threshold, FailureLimit: failLimit, ProgressTicks: ticks,
			Status: model.StatusPending, CreatedAt: now,
		}
		if err := tx.CreateRollout(r.ctx, ro); err != nil {
			return err
		}
		for i := 0; i < initialOld; i++ {
			pid, err := r.pm.Start("w"+appID[4:], "v1")
			if err != nil {
				return err
			}
			in := &model.Instance{
				ID: "inst_" + pid, AppID: appID, RolloutID: ids.New("rol"),
				RevisionID: oldRev, ProcID: pid, Phase: model.PhaseStarting,
				ReadyStreak: 0, CreatedAt: now,
			}
			if err := tx.InstanceCreate(r.ctx, in); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	return
}

// warmOldReady 让初始旧实例探针达到就绪阈值。
func (r *rig) warmOldReady(appID string, threshold int) {
	insts, err := r.st.InstancesByApp(r.ctx, appID, true)
	if err != nil {
		r.t.Fatalf("instances: %v", err)
	}
	for range threshold + 1 {
		for _, in := range insts {
			if _, err := r.pm.Status(in.ProcID); err != nil {
				r.t.Fatalf("warm status: %v", err)
			}
		}
	}
	if err := r.st.WithTx(r.ctx, func(tx store.Tx) error {
		insts, err := tx.InstancesByApp(r.ctx, appID, true)
		if err != nil {
			return err
		}
		for _, in := range insts {
			in.Phase = model.PhaseReady
			in.ReadyStreak = threshold
			if err := tx.InstanceUpdate(r.ctx, in); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		r.t.Fatalf("warm update: %v", err)
	}
}

func (r *rig) runUntil(rolloutID string, maxTicks int) *model.Rollout {
	t := r.t
	for i := 0; i < maxTicks; i++ {
		res, err := r.ctrl.Tick(r.ctx, rolloutID, "test")
		if err != nil {
			t.Fatalf("tick %d: %v", i, err)
		}
		if !res.StillInFlight {
			break
		}
	}
	ro, err := r.st.RolloutGet(r.ctx, rolloutID)
	if err != nil {
		t.Fatalf("get rollout: %v", err)
	}
	return ro
}

// independentCount 用测试自己的循环统计快照（不依赖被测实现的计数）。
func (r *rig) independentCount(appID, targetRev string) (active, newActive, oldActive, available, newAvail, oldAvail int) {
	insts, err := r.st.InstancesByApp(r.ctx, appID, true)
	if err != nil {
		r.t.Fatalf("count instances: %v", err)
	}
	for _, in := range insts {
		active++
		if in.RevisionID == targetRev {
			newActive++
		} else {
			oldActive++
		}
		if in.Phase == model.PhaseReady {
			available++
			if in.RevisionID == targetRev {
				newAvail++
			} else {
				oldAvail++
			}
		}
	}
	return
}

func eventKinds(t *testing.T, st *store.Store, rolloutID string) []string {
	t.Helper()
	evs, err := st.ListEventsByRollout(context.Background(), rolloutID)
	if err != nil {
		t.Fatalf("events: %v", err)
	}
	kinds := make([]string, 0, len(evs))
	for _, e := range evs {
		kinds = append(kinds, e.Kind)
	}
	return kinds
}
