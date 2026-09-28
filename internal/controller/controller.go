// Package controller 实现滚动发布的协调循环（核心机制）。
//
// 设计原则：
//   - 每个滴答最多产生一个有效写动作（start_new 或 remove_old），
//     因此“每一步”的副本约束可以用事件快照逐步断言；
//   - 先扩容新版本（受 maxSurge 约束），新版本就绪后才缩容旧版本，
//     任何缩容后可用数都不得低于 D-maxUnavailable —— 顺序可解释、可复核；
//   - 创建成功只代表进程存在，只有连续探针成功达到阈值才计入可用；
//   - 失败实例永不计入可用，并按确定类别终止发布。
package controller

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"rollingdeploy/internal/model"
	"rollingdeploy/internal/procman"
)

// TxFace 是单个协调滴答内可用的持久化能力（由 store 的事务结构匹配实现）。
type TxFace interface {
	AppGet(ctx context.Context, id string) (*model.App, error)
	RolloutGet(ctx context.Context, id string) (*model.Rollout, error)
	RolloutUpdate(ctx context.Context, r *model.Rollout) error
	RevisionGet(ctx context.Context, id string) (*model.Revision, error)
	InstancesByApp(ctx context.Context, appID string, activeOnly bool) ([]*model.Instance, error)
	InstanceCreate(ctx context.Context, in *model.Instance) error
	InstanceUpdate(ctx context.Context, in *model.Instance) error
	EventInsert(ctx context.Context, e *model.Event) (int64, error)
}

// TxFunc 在一个事务中执行协调动作。
type TxFunc func(ctx context.Context, fn func(TxFace) error) error

// ProcStatus 是控制器对进程管理器观测结果的中立表示。
type ProcStatus struct {
	Missing bool // 控制器有引用，但模拟器里不存在（宿主重启等）
	Ready   bool
}

// ProcessManager 是协调循环依赖的唯一外部参与者端口。
type ProcessManager interface {
	Start(appName, version string) (procID string, err error)
	Status(procID string) (ProcStatus, error)
	Stop(procID string) error
}

// TickResult 描述一次滴答的结论（供 API 与日志解释）。
type TickResult struct {
	RolloutID       string `json:"rollout_id"`
	Status          string `json:"status"`
	FailureCategory string `json:"failure_category,omitempty"`
	FailureReason   string `json:"failure_reason,omitempty"`
	Tick            int    `json:"tick"`
	// StillInFlight 表示滴答后发布仍在推进（调用方据此决定是否继续）
	StillInFlight bool `json:"still_in_flight"`
}

// Controller 执行协调。
type Controller struct {
	tx  TxFunc
	pm  ProcessManager
	log *slog.Logger
}

// New 创建控制器。
func New(tx TxFunc, pm ProcessManager, log *slog.Logger) *Controller {
	if log == nil {
		log = slog.Default()
	}
	return &Controller{tx: tx, pm: pm, log: log}
}

// ErrConflict 表示发布状态已被其他执行者改变（例如已终态），本次滴答跳过。
var ErrConflict = errors.New("controller: rollout not in flight")

// capacityRejectLimit 是容量池连续拒绝创建多少次后判定容量不足终态；
// 此前视为瞬时拥塞，配合进度截止时间等待外部释放容量。
const capacityRejectLimit = 3

type tickState struct {
	ctx      context.Context
	reqID    string
	app      *model.App
	rollout  *model.Rollout
	target   *model.Revision
	insts    []*model.Instance
	failNote string // 本滴答观测阶段最近一次新实例失败细节
}

func (c *Controller) snap(ts *tickState) model.Snapshot {
	return model.ComputeSnapshot(ts.insts, ts.rollout.RevisionID,
		ts.rollout.Replicas, ts.rollout.MaxSurge, ts.rollout.MaxUnavailable, ts.rollout.Ticks)
}

func (c *Controller) eventf(ts *tickState, t TxFace, kind, instanceID, note string) {
	snap := c.snap(ts)
	_, err := t.EventInsert(ts.ctx, &model.Event{
		RequestID:  ts.reqID,
		RolloutID:  ts.rollout.ID,
		AppID:      ts.app.ID,
		Kind:       kind,
		RevisionID: ts.rollout.RevisionID,
		InstanceID: instanceID,
		Note:       note,
		Snapshot:   snap,
	})
	if err != nil {
		c.log.Error("insert event failed", "rollout", ts.rollout.ID, "kind", kind, "err", err)
	}
}

// Tick 推进指定发布一个协调滴答。已终态时返回 ErrConflict。
func (c *Controller) Tick(ctx context.Context, rolloutID, requestID string) (TickResult, error) {
	res := TickResult{RolloutID: rolloutID}
	if requestID == "" {
		requestID = "tick:" + rolloutID
	}
	err := c.tx(ctx, func(t TxFace) error {
		ts := &tickState{ctx: ctx, reqID: requestID}
		r, err := t.RolloutGet(ctx, rolloutID)
		if err != nil {
			return err
		}
		if !r.InFlight() {
			return ErrConflict
		}
		ts.rollout = r
		ts.app, err = t.AppGet(ctx, r.AppID)
		if err != nil {
			return err
		}
		ts.target, err = t.RevisionGet(ctx, r.RevisionID)
		if err != nil {
			return err
		}
		ts.insts, err = t.InstancesByApp(ctx, ts.app.ID, true)
		if err != nil {
			return err
		}
		r.Ticks++
		r.Status = model.StatusRunning
		if err := t.RolloutUpdate(ctx, r); err != nil {
			return err
		}
		c.eventf(ts, t, model.EvTickBegin, "", "reconcile tick begins")

		// 1) 重新观测全部现存实例（这也是控制器重启后的重新挂载步骤）。
		finished, err := c.observe(ts, t)
		if err != nil {
			return err
		}
		snap := c.snap(ts)
		c.log.Info("tick observed",
			"request_id", requestID, "rollout", rolloutID, "tick", r.Ticks,
			"active", snap.TotalActive, "available", snap.Available,
			"new_avail", snap.NewAvailable, "old_avail", snap.OldAvailable)
		if !finished {
			finished, err = c.act(ts, t)
			if err != nil {
				return err
			}
		}
		// 重新计算终态与快照。
		if err := t.RolloutUpdate(ctx, ts.rollout); err != nil {
			return err
		}
		res.Tick = ts.rollout.Ticks
		res.Status = ts.rollout.Status
		res.FailureCategory = ts.rollout.FailureCategory
		res.FailureReason = ts.rollout.FailureReason
		res.StillInFlight = ts.rollout.InFlight()
		return nil
	})
	return res, err
}

// observe 对每个活跃实例做一次探针观测，更新阶段与就绪连续计数。
// 返回 finished=true 表示本滴答已将发布终态化（失败），不应再执行伸缩动作。
func (c *Controller) observe(ts *tickState, t TxFace) (bool, error) {
	r := ts.rollout
	for _, in := range ts.insts {
		if !model.IsActive(in.Phase) {
			continue
		}
		// 已判定失败的实例结论已知，不重复探测、不重复计入失败预算。
		if in.Phase == model.PhaseFailed {
			continue
		}
		st, err := c.pm.Status(in.ProcID)
		switch {
		case st.Missing:
			// 模拟器中找不到进程：宿主重启丢失。先改账（新实例失败即回收释放槽位），
			// 再发射事件，保证事件快照是“步骤应用后”的视图。
			note := fmt.Sprintf("process %s missing in process manager (host restart?)", in.ProcID)
			wasNew := in.RevisionID == r.RevisionID
			c.applyFailedInstance(ts, t, in, note)
			c.eventf(ts, t, model.EvReattach, in.ID,
				reattachNote(note, wasNew))
		case procman.IsStartError(err):
			// 启动后崩溃：确定的实例级失败，绝不计入可用。
			note := fmt.Sprintf("process %s crashed: %v", in.ProcID, err)
			// 先记录失败（新版本失败在预算内立即回收），再发事件。
			c.applyFailedInstance(ts, t, in, note)
			c.eventf(ts, t, model.EvStartFailed, in.ID, note)
		case err != nil:
			// 未知探针错误：不据此判失败，保留不确定结论并继续等待。
			c.log.Warn("probe inconclusive",
				"request_id", ts.reqID, "instance", in.ID, "err", err)
			continue
		default:
			wasReady := in.Phase == model.PhaseReady
			if st.Ready {
				in.ReadyStreak++
				if in.ReadyStreak >= r.ReadyThreshold {
					if !wasReady {
						in.Phase = model.PhaseReady
						if err := t.InstanceUpdate(ts.ctx, in); err != nil {
							return false, err
						}
						c.eventf(ts, t, model.EvBecomeReady, in.ID,
							fmt.Sprintf("ready streak %d reached threshold %d", in.ReadyStreak, r.ReadyThreshold))
					} else {
						if err := t.InstanceUpdate(ts.ctx, in); err != nil {
							return false, err
						}
					}
				} else {
					// 探针成功但还没持续到阈值：仍是 starting，不算可用。
					if in.Phase != model.PhaseStarting {
						in.Phase = model.PhaseStarting
					}
					if err := t.InstanceUpdate(ts.ctx, in); err != nil {
						return false, err
					}
				}
			} else {
				// 就绪抖动：连续计数清零，曾经就绪的实例被移出可用并单独记录。
				// 先改账再发事件，使事件快照反映降级后的真实可用数。
				demoted := wasReady
				in.ReadyStreak = 0
				in.Phase = model.PhaseStarting
				if err := t.InstanceUpdate(ts.ctx, in); err != nil {
					return false, err
				}
				if demoted {
					c.eventf(ts, t, model.EvProbeDemoted, in.ID,
						"readiness probe failed after being ready; streak reset")
				}
			}
		}
		// 失败数在观测中途达到上限：当拍立即终态化，不再探测/调度其他实例。
		if r.InFlight() && r.FailureCount >= r.FailureLimit {
			c.failRollout(ts, t, model.FailStartFailure, ts.failNote)
			return true, nil
		}
	}
	return false, nil
}

// reattachNote 在宿主丢失说明后追加该实例属于哪个版本，便于区分影响面。
func reattachNote(base string, wasNew bool) string {
	if wasNew {
		return base + " (instance belonged to target revision; failed and slot reclaimed)"
	}
	return base + " (instance belonged to old revision)"
}

// applyFailedInstance 统一处理“观测阶段发现的失败实例”：
// 新版本失败计入失败预算，并立即终止其进程/释放副本槽（它是已死的新实例，
// 不能占着 surge 槽位阻碍替补创建）；旧版本失败不消耗新发布的失败预算，
// 保留为 failed 阶段并由缩容步骤按“先回收失败实例”的顺序处理。
// 无论哪种，failed 绝不计入可用。本函数只改账，事件由调用方在改账后发射。
func (c *Controller) applyFailedInstance(ts *tickState, t TxFace, in *model.Instance, note string) {
	r := ts.rollout
	in.Phase = model.PhaseFailed
	in.ReadyStreak = 0
	if err := t.InstanceUpdate(ts.ctx, in); err != nil {
		c.log.Error("mark instance failed", "instance", in.ID, "err", err)
		return
	}
	if in.RevisionID == r.RevisionID {
		r.FailureCount++
		ts.failNote = note
		c.log.Warn("new revision instance failed",
			"request_id", ts.reqID, "rollout", r.ID, "instance", in.ID,
			"failure_count", r.FailureCount, "limit", r.FailureLimit, "note", note)
		// 终止死亡新实例并从活跃视图摘除，下一拍即可在容量内创建替补。
		_ = c.pm.Stop(in.ProcID)
		in.Phase = model.PhaseTerminated
		if err := t.InstanceUpdate(ts.ctx, in); err != nil {
			c.log.Error("reap failed new instance", "instance", in.ID, "err", err)
			return
		}
		filtered := ts.insts[:0]
		for _, x := range ts.insts {
			if x.ID != in.ID {
				filtered = append(filtered, x)
			}
		}
		ts.insts = filtered
	} else {
		c.log.Warn("old revision instance failed (not charged to rollout failure budget)",
			"request_id", ts.reqID, "rollout", r.ID, "instance", in.ID, "note", note)
	}
}

// act 在观测之后执行至多一个有效伸缩动作。返回 finished=true 表示发布已终态化。
func (c *Controller) act(ts *tickState, t TxFace) (bool, error) {
	r := ts.rollout
	snap := c.snap(ts)

	// 成功条件：旧版本全部退场，且可用数达到期望。
	if snap.OldActive == 0 && snap.NewAvailable >= r.Replicas {
		c.succeed(ts, t)
		return true, nil
	}

	// 进度截止时间：超时是“不确定结论”，与确定的实例失败分列。
	if r.ProgressTicks > 0 && r.Ticks >= r.ProgressTicks {
		cat := model.FailReadinessTimeout
		if r.CapacityStreak > 0 {
			cat = model.FailInsufficientCapacity
		}
		c.failRollout(ts, t, cat, fmt.Sprintf(
			"progress deadline %d ticks exceeded; capacity_streak=%d, new_available=%d/%d",
			r.ProgressTicks, r.CapacityStreak, snap.NewAvailable, r.Replicas))
		return true, nil
	}

	// 动作 1：在 maxSurge 容量内创建新版本实例。
	if snap.NewActive < r.Replicas && snap.TotalActive < snap.MaxTotal {
		return c.startOne(ts, t, snap)
	}

	// 动作 2：缩容一个旧版本实例（surge 已满，需要换出槽位；
	// 或容量池拒绝过创建，需要回收旧进程腾位置）。
	if snap.OldActive > 0 {
		done, blocked, err := c.removeOneOld(ts, t, snap)
		if err != nil {
			return false, err
		}
		if blocked {
			// 容量池拒绝过创建，且旧实例受最小可用线保护无法回收。
			// 连续拒绝达到宽限上限才判定环境容量不足；此前按瞬时拥塞等待重试。
			if r.CapacityStreak >= capacityRejectLimit {
				c.failRollout(ts, t, model.FailInsufficientCapacity,
					"capacity pool rejects creates and no old instance can be removed without breaching minAvailable")
				return true, nil
			}
			return false, nil
		}
		return done, nil
	}

	// 既不能扩也没有旧实例可缩：
	// maxSurge=0 且 maxUnavailable=0 时零推进，是确定的策略配置错误。
	if snap.NewActive == 0 {
		c.failRollout(ts, t, model.FailInvalidStrategy,
			"no progress possible with maxSurge=0 and maxUnavailable=0 (need a surge slot or unavailable budget)")
		return true, nil
	}

	// 新版本实例已在途，等待探针达到阈值 —— 这是正常等待，本滴答不再动作。
	c.log.Info("awaiting readiness",
		"request_id", ts.reqID, "rollout", r.ID, "tick", r.Ticks,
		"new_active", snap.NewActive, "new_available", snap.NewAvailable)
	return false, nil
}

// startOne 创建一个新版本实例，并处理创建被拒绝 / 启动即失败。
func (c *Controller) startOne(ts *tickState, t TxFace, snap model.Snapshot) (bool, error) {
	r := ts.rollout
	procID, err := c.pm.Start(ts.app.Name, ts.target.Version)
	switch {
	case errors.Is(err, procman.ErrCapacity):
		r.CapacityStreak++
		c.eventf(ts, t, model.EvStartRejected, "",
			fmt.Sprintf("synthetic capacity pool rejected create; streak=%d", r.CapacityStreak))
		c.log.Warn("create rejected: capacity exhausted",
			"request_id", ts.reqID, "rollout", r.ID, "tick", r.Ticks, "streak", r.CapacityStreak)
		// 容量满：若能在不击穿最小可用线的前提下回收一个旧实例，则本拍回收，
		// 下一拍创建即可成功（可恢复的容量不足夹具）。
		snap := c.snap(ts)
		if snap.OldActive > 0 {
			done, blocked, err := c.removeOneOld(ts, t, snap)
			if err != nil {
				return false, err
			}
			if blocked {
				// 容量拒绝过创建，且旧实例受最小可用线保护无法回收：
				// 确定的环境容量不足，而非策略配置错误。
				c.failRollout(ts, t, model.FailInsufficientCapacity,
					"capacity pool rejects creates and no old instance can be removed without breaching minAvailable")
				return true, nil
			}
			return done, nil
		}
		return false, nil
	case err != nil:
		// 启动即失败：不留存活进程。失败计入预算并产生确定的失败事件。
		r.FailureCount++
		r.CapacityStreak = 0
		ts.failNote = fmt.Sprintf("start returned error: %v", err)
		c.eventf(ts, t, model.EvStartFailed, "", ts.failNote)
		c.log.Error("new instance failed to start",
			"request_id", ts.reqID, "rollout", r.ID, "tick", r.Ticks,
			"failure_count", r.FailureCount, "err", err)
		if r.FailureCount >= r.FailureLimit {
			c.failRollout(ts, t, model.FailStartFailure, ts.failNote)
			return true, nil
		}
		return false, nil
	}

	// 创建成功 != 就绪：以 starting 阶段、连续计数 0 落账。
	r.CapacityStreak = 0
	in := &model.Instance{
		ID:         "inst_" + procID,
		AppID:      ts.app.ID,
		RolloutID:  r.ID,
		RevisionID: r.RevisionID,
		ProcID:     procID,
		Phase:      model.PhaseStarting,
	}
	if err := t.InstanceCreate(ts.ctx, in); err != nil {
		// 落账失败：尽力回收已创建进程，避免泄漏。
		_ = c.pm.Stop(procID)
		return false, err
	}
	ts.insts = append(ts.insts, in)
	c.eventf(ts, t, model.EvStartNew, in.ID,
		fmt.Sprintf("created process %s at version %s (phase=starting, NOT available)", procID, ts.target.Version))
	c.log.Info("started new instance",
		"request_id", ts.reqID, "rollout", r.ID, "tick", r.Ticks,
		"instance", in.ID, "version", ts.target.Version)
	return false, nil
}

// pickOldVictim 按可解释的顺序挑选待缩容旧实例：
// 先回收已失败（不影响可用），其次 starting，最后 ready；
// ready 实例只有在回收后可用数仍不低于最小可用线时才可回收。
func pickOldVictim(insts []*model.Instance, targetRevID string, snap model.Snapshot) *model.Instance {
	for _, ph := range []string{model.PhaseFailed, model.PhaseStarting, model.PhaseReady} {
		for _, in := range insts {
			if !model.IsActive(in.Phase) || in.RevisionID == targetRevID || in.Phase != ph {
				continue
			}
			if ph == model.PhaseReady && snap.Available-1 < snap.MinAvailable {
				continue
			}
			return in
		}
	}
	return nil
}

// removeOneOld 缩容一个旧版本实例。
// 顺序可解释：优先回收已失败的旧实例（不影响可用），其次 starting，最后 ready；
// 回收 ready 实例后可用数仍必须 >= D-maxUnavailable。
// removeOneOld 缩容一个旧版本实例。
// 返回 blocked=true 表示存在旧实例但受最小可用线保护无法回收。
// done 仅在发布被终态化时为 true。
func (c *Controller) removeOneOld(ts *tickState, t TxFace, snap model.Snapshot) (done, blocked bool, err error) {
	r := ts.rollout
	victim := pickOldVictim(ts.insts, r.RevisionID, snap)
	if victim == nil {
		// 有旧实例，但回收任何一个都会击穿最小可用线：
		// 若此前发生过容量拒绝，这是容量死锁（由调用方终态化）；
		// 若有新版本实例在途，则等待它就绪；
		// 若根本没有新版本实例在途，说明 maxSurge=0、maxUnavailable=0 —— 策略配置错误。
		if r.CapacityStreak > 0 {
			return false, true, nil
		}
		if snap.NewActive == 0 {
			c.failRollout(ts, t, model.FailInvalidStrategy,
				"cannot scale out (no surge budget) and cannot scale in (removal would breach minAvailable): need maxSurge>0 or maxUnavailable>0")
			return true, false, nil
		}
		c.log.Info("cannot remove old without breaching minAvailable; waiting",
			"request_id", ts.reqID, "rollout", r.ID, "tick", r.Ticks,
			"available", snap.Available, "min", snap.MinAvailable)
		return false, false, nil
	}

	if err := c.pm.Stop(victim.ProcID); err != nil && !errors.Is(err, procman.ErrCapacity) {
		// Stop 找不到进程也算成功（可能已随宿主消失）；其他错误记录但不改账。
		c.log.Warn("stop process returned error",
			"request_id", ts.reqID, "instance", victim.ID, "proc", victim.ProcID, "err", err)
	}
	victim.Phase = model.PhaseTerminated
	victim.ReadyStreak = 0
	if err := t.InstanceUpdate(ts.ctx, victim); err != nil {
		return false, false, err
	}
	// 从活跃视图移除（审计行仍在数据库）。
	filtered := ts.insts[:0]
	for _, in := range ts.insts {
		if in.ID != victim.ID {
			filtered = append(filtered, in)
		}
	}
	ts.insts = filtered
	r.CapacityStreak = 0
	note := fmt.Sprintf("scaled in old revision instance %s (proc %s); new_available=%d before removal",
		victim.ID, victim.ProcID, snap.NewAvailable)
	c.eventf(ts, t, model.EvRemoveOld, victim.ID, note)
	c.log.Info("removed old instance",
		"request_id", ts.reqID, "rollout", r.ID, "tick", r.Ticks,
		"instance", victim.ID, "available_after", c.snap(ts).Available)
	return false, false, nil
}

func (c *Controller) succeed(ts *tickState, t TxFace) {
	now := time.Now()
	r := ts.rollout
	r.Status = model.StatusSucceeded
	r.FailureCategory = ""
	r.FailureReason = ""
	r.FinishedAt = &now
	c.eventf(ts, t, model.EvRolloutDone, "",
		fmt.Sprintf("rollout succeeded at version %s with %d available", ts.target.Version, r.Replicas))
	c.log.Info("rollout succeeded",
		"request_id", ts.reqID, "rollout", r.ID, "ticks", r.Ticks, "version", ts.target.Version)
}

func (c *Controller) failRollout(ts *tickState, t TxFace, category, reason string) {
	now := time.Now()
	r := ts.rollout
	r.Status = model.StatusFailed
	r.FailureCategory = category
	r.FailureReason = reason
	r.FinishedAt = &now
	c.eventf(ts, t, model.EvRolloutFail, "",
		fmt.Sprintf("category=%s reason=%s", category, reason))
	// 失败原因与不确定结论单列：日志级别按类别区分。
	args := []any{"request_id", ts.reqID, "rollout", r.ID, "category", category,
		"ticks", r.Ticks, "reason", reason}
	if category == model.FailReadinessTimeout {
		c.log.Warn("rollout failed with inconclusive outcome (deadline exceeded)", args...)
	} else {
		c.log.Error("rollout failed", args...)
	}
}
