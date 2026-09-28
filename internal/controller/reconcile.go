package controller

import (
	"context"
	"errors"
	"fmt"

	"rollctl/internal/adapter"
	"rollctl/internal/model"
)

// reconcileWorkload performs one reconcile step for one workload. The caller
// (Tick) holds c.mu and has already advanced the clock.
func (c *Controller) reconcileWorkload(ctx context.Context, name string, tick int64) (WorkloadTick, error) {
	wt := WorkloadTick{Workload: name}

	w, err := c.st.GetWorkload(ctx, name)
	if err != nil {
		return wt, err
	}
	wt.Baseline = w.Replicas

	instances, err := c.st.ListInstances(ctx, name)
	if err != nil {
		return wt, err
	}

	// Resolve the release this step works against (pending becomes active on
	// its first observed tick; bootstrap/rollout/rollback all flow the same
	// machine — rollback is just another new release).
	rel, err := c.st.PendingOrActiveRelease(ctx, name)
	if err != nil {
		return wt, err
	}
	threshold := 1
	deadline := int64(20)
	if rel != nil {
		threshold = rel.Policy.ReadyThresholdTicks
		deadline = rel.Policy.DeadlineTicks
		if threshold <= 0 {
			threshold = 1
		}
		if deadline <= 0 {
			deadline = 20
		}
		if rel.State == model.RelPending {
			rel.State = model.RelActive
			rel.StartedTick = tick
			rel.LastProgressTick = tick
			if err := c.st.UpdateRelease(ctx, rel); err != nil {
				return wt, err
			}
			ev := c.mkEvent(tick, name, rel, model.LevelInfo, model.EvReleaseActivated,
				"release %s activated for revision %s (surge=%d unavailable=%d readyThreshold=%d deadline=%d)",
				rel.ID, rel.Revision, rel.Policy.MaxSurge, rel.Policy.MaxUnavailable, threshold, deadline)
			if _, err := c.st.AppendEvent(ctx, ev); err != nil {
				return wt, err
			}
			wt.Events = append(wt.Events, ev)
		}
		wt.ActiveRelease = rel.ID
	}

	// ---- Phase 1: observation housekeeping. No serving-count change. ----
	sawFlap := false
	for _, ins := range instances {
		if !ins.Live() || ins.ProcID == "" {
			continue
		}
		status, oerr := c.pm.Observe(ctx, ins.ProcID)
		switch {
		case errors.Is(oerr, adapter.ProcGone):
			// Process vanished without an observed exit.
			if err := c.markFailed(ctx, ins, model.FailRuntimeExited, "process handle disappeared", tick, rel, &wt); err != nil {
				return wt, err
			}
			sawFlap = isNewOf(ins, rel)
		case oerr != nil:
			return wt, fmt.Errorf("observe instance %s: %w", ins.ID, oerr)
		case !status.Running && status.Reason == adapter.ReasonExited:
			cat := model.FailRuntimeExited
			if ins.State == model.StateStarting {
				cat = model.FailStartFailed
			}
			msg := "process exited (fixture)"
			if err := c.markFailed(ctx, ins, cat, msg, tick, rel, &wt); err != nil {
				return wt, err
			}
			if isNewOf(ins, rel) {
				sawFlap = false // this is a start failure, not jitter
			}
			if err := c.pm.Terminate(ctx, ins.ProcID); err != nil && !errors.Is(err, adapter.ProcGone) {
				return wt, err
			}
		case status.Running && status.Ready:
			ins.ReadyStreak++
			if ins.State != model.StateReady && ins.ReadyStreak >= threshold {
				ins.State = model.StateReady
				ins.ReadyTick = tick
				ins.ReadyStreak = threshold
				ins.UpdatedAt = c.now()
				if err := c.st.UpdateInstance(ctx, ins); err != nil {
					return wt, err
				}
				ev := c.mkEvent(tick, name, rel, model.LevelInfo, model.EvInstanceReady,
					"instance %s ready on revision %s after %d consecutive ready observations", ins.ID, ins.Revision, threshold)
				ev.InstanceID, ev.Revision = ins.ID, ins.Revision
				if _, err := c.st.AppendEvent(ctx, ev); err != nil {
					return wt, err
				}
				wt.Events = append(wt.Events, ev)
			} else {
				ins.UpdatedAt = c.now()
				if err := c.st.UpdateInstance(ctx, ins); err != nil {
					return wt, err
				}
			}
		case status.Running && !status.Ready:
			// Running but not ready (still ramping, or jitter).
			if ins.State == model.StateReady {
				// Readiness lost after promotion: this is the jitter signal.
				ins.State = model.StateStarting
				ins.ReadyTick = 0
				ins.ReadyStreak = 0
				ins.UpdatedAt = c.now()
				if err := c.st.UpdateInstance(ctx, ins); err != nil {
					return wt, err
				}
				ev := c.mkEvent(tick, name, rel, model.LevelWarn, model.EvInstanceReadyLost,
					"instance %s lost readiness on revision %s (%s); it no longer counts as available",
					ins.ID, ins.Revision, orDefault(status.Reason, "not ready"))
				ev.InstanceID, ev.Revision = ins.ID, ins.Revision
				ev.Certain = false
				if _, err := c.st.AppendEvent(ctx, ev); err != nil {
					return wt, err
				}
				wt.Events = append(wt.Events, ev)
				if isNewOf(ins, rel) {
					sawFlap = true
				}
			} else if ins.ReadyStreak != 0 {
				// Was accumulating ready observations, got a not-ready one: streak resets.
				ins.ReadyStreak = 0
				ins.UpdatedAt = c.now()
				if err := c.st.UpdateInstance(ctx, ins); err != nil {
					return wt, err
				}
				if isNewOf(ins, rel) {
					ev := c.mkEvent(tick, name, rel, model.LevelWarn, model.EvInstanceNotReady,
						"instance %s readiness streak reset on revision %s (%s)", ins.ID, ins.Revision,
						orDefault(status.Reason, "not ready"))
					ev.InstanceID, ev.Revision = ins.ID, ins.Revision
					ev.Certain = false
					if _, err := c.st.AppendEvent(ctx, ev); err != nil {
						return wt, err
					}
					wt.Events = append(wt.Events, ev)
					sawFlap = true
				}
			}
		}
	}

	if rel != nil && sawFlap {
		rel.SawFlap = true
	}

	// Re-read instances after observations.
	instances, err = c.st.ListInstances(ctx, name)
	if err != nil {
		return wt, err
	}
	fillCounts(&wt, instances, rel)

	// ---- Phases 2-3: plan. ----
	if rel == nil {
		if err := c.reconcileSteady(ctx, w, instances, tick); err != nil {
			return wt, err
		}
		instances, err = c.st.ListInstances(ctx, name)
		if err != nil {
			return wt, err
		}
		fillCounts(&wt, instances, nil)
		return wt, nil
	}

	if err := c.reconcileRollout(ctx, w, rel, instances, tick, deadline, &wt); err != nil {
		return wt, err
	}

	instances, err = c.st.ListInstances(ctx, name)
	if err != nil {
		return wt, err
	}
	fillCounts(&wt, instances, rel)
	return wt, nil
}

// markFailed moves an instance to failed and appends an event.
func (c *Controller) markFailed(ctx context.Context, ins *model.Instance, cat model.FailureCategory, msg string, tick int64, rel *model.Release, wt *WorkloadTick) error {
	ins.State = model.StateFailed
	ins.FailCategory = cat
	ins.FailMessage = msg
	ins.FailedTick = tick
	ins.ReadyTick = 0
	ins.ReadyStreak = 0
	ins.UpdatedAt = c.now()
	if err := c.st.UpdateInstance(ctx, ins); err != nil {
		return err
	}
	ev := c.mkEvent(tick, ins.Workload, rel, model.LevelFail, model.EvInstanceExited,
		"instance %s failed on revision %s: %s", ins.ID, ins.Revision, msg)
	ev.InstanceID, ev.Revision, ev.Category = ins.ID, ins.Revision, string(cat)
	if cat == model.FailStartFailed {
		ev.Type = model.EvInstanceStartFailed
	}
	if _, err := c.st.AppendEvent(ctx, ev); err != nil {
		return err
	}
	wt.Events = append(wt.Events, ev)
	return nil
}

// reconcileSteady handles a workload with no in-flight release: keep the
// current revision at the desired replica count.
func (c *Controller) reconcileSteady(ctx context.Context, w *model.Workload, instances []*model.Instance, tick int64) error {
	live := 0
	for _, ins := range instances {
		if ins.Live() {
			live++
		}
	}
	if live >= w.Replicas {
		return nil
	}
	procID, serr := c.pm.Start(ctx, adapter.StartMeta{Workload: w.Name, Revision: w.CurrentRevision, ReleaseID: w.CurrentReleaseID})
	if serr != nil {
		// Steady state: capacity exhaustion or a rejected start is retried on
		// later ticks; surface it as a warn event rather than failing the step.
		ev := c.mkEvent(tick, w.Name, nil, model.LevelWarn, model.EvWaitReady,
			"steady-state start for revision %s not possible yet: %v", w.CurrentRevision, serr)
		ev.Certain = false
		_, err := c.st.AppendEvent(ctx, ev)
		return err
	}
	if err := c.insertStartingInstance(ctx, w.Name, w.CurrentRevision, w.CurrentReleaseID, procID, tick); err != nil {
		return err
	}
	ev := c.mkEvent(tick, w.Name, nil, model.LevelInfo, model.EvSteadyStart,
		"steady state: started replacement instance on revision %s (proc=%s); created, NOT ready yet", w.CurrentRevision, procID)
	ev.Revision = w.CurrentRevision
	_, err := c.st.AppendEvent(ctx, ev)
	return err
}

// reconcileRollout is the rolling-update state machine. Exactly one structural
// action is attempted per tick; if none is safe/available the tick may wait.
//
// Ordering (the explainable part):
//
//	first bring new instances up within the surge budget (a slot may be freed
//	by removing one old instance only when the availability invariant allows
//	it — this is the surge=0, capacity-full case);
//	once new available replicas cover the baseline, remove old instances one at
//	a time, each removal freeing a slot for the next new instance;
//	only when every old instance is gone does the release succeed.
//
// "Created" (a process handle returned by Start) is never readiness: an
// instance counts toward nothing until threshold-consecutive ready
// observations promote it.
func (c *Controller) reconcileRollout(ctx context.Context, w *model.Workload, rel *model.Release, instances []*model.Instance, tick, deadline int64, wt *WorkloadTick) error {
	baseline := w.Replicas
	newFailed := 0
	var newLive, newAvail, oldLive, oldAvail int
	newAllStable := true
	sawNewLive := false
	for _, ins := range instances {
		switch {
		case ins.Revision == rel.Revision:
			if ins.State == model.StateFailed {
				newFailed++
			}
			if ins.Live() {
				newLive++
				sawNewLive = true
				if ins.State != model.StateReady {
					newAllStable = false
				}
			}
			if ins.Available() {
				newAvail++
			}
		default:
			if ins.Live() {
				oldLive++
			}
			if ins.Available() {
				oldAvail++
			}
		}
	}
	// A readiness blip freezes old scale-down; once every new live replica is
	// stably ready again, the freeze lifts so a recovered jitter does not
	// permanently kill a healthy release.
	if sawNewLive && newAllStable {
		rel.SawFlap = false
	}

	// Zero-diff rollout (e.g. rollback to the revision that already serves all
	// traffic): nothing to move.
	if newAvail >= baseline && oldLive == 0 {
		return c.succeedRelease(ctx, w, rel, tick, wt)
	}

	// Start-failure budget: a failed new instance is a hard fact.
	if newFailed > rel.Policy.MaxStartFailures {
		rel.FailureCategory = model.FailStartFailed
		rel.FailMessage = fmt.Sprintf("%d new-instance start failure(s) exceeded budget %d", newFailed, rel.Policy.MaxStartFailures)
		return c.failRelease(ctx, w, rel, tick, wt)
	}

	// No-progress deadline: structural changes and new availability gains count
	// as progress; readiness jitter does not.
	if rel.LastProgressTick == 0 {
		rel.LastProgressTick = tick
	}
	if tick-rel.LastProgressTick >= deadline {
		cat := model.FailRolloutStalled
		if rel.SawCapacityBlock {
			cat = model.FailInsufficientCapacity
		} else if rel.SawFlap {
			cat = model.FailReadinessFlapping
		}
		rel.FailureCategory = cat
		rel.FailMessage = fmt.Sprintf("no structural progress for %d ticks (deadline=%d); sawFlap=%v sawCapacityBlock=%v",
			tick-rel.LastProgressTick, deadline, rel.SawFlap, rel.SawCapacityBlock)
		return c.failRelease(ctx, w, rel, tick, wt)
	}

	availTotal := newAvail + oldAvail
	liveTotal := newLive + oldLive
	minAvail := baseline - rel.Policy.MaxUnavailable
	if minAvail < 0 {
		minAvail = 0
	}
	maxLive := baseline + rel.Policy.MaxSurge
	needNew := newLive < baseline

	acted := false

	switch {
	case needNew && liveTotal < maxLive:
		// 1a) Surge room exists: bring up a new instance (created != ready;
		// only observations promote it).
		started, blocked, err := c.tryStartNew(ctx, w, rel, tick, wt)
		if err != nil {
			return err
		}
		if started {
			acted = true
			break
		}
		if blocked {
			// 1b) Capacity fixture refuses the start. Free exactly one slot by
			// removing an old instance, but only while the availability
			// invariant survives and only while new replicas have shown no
			// readiness instability (never sacrifice proven old capacity for
			// an unproven new replica).
			rel.SawCapacityBlock = true
			if !rel.SawFlap && oldLive > 0 && availTotal > minAvail {
				if err := c.terminateOldestOld(ctx, instances, rel, tick, wt); err != nil {
					return err
				}
				acted = true
			} else {
				why := "removing an old instance would drop available to/below minAvailable"
				if rel.SawFlap {
					why = "new replicas already showed readiness instability; old capacity is frozen"
				}
				if err := c.blockedEvent(ctx, rel, tick, wt,
					"capacity fixture full (live=%d) and %s (available=%d minAvailable=%d)",
					liveTotal, why, availTotal, minAvail); err != nil {
					return err
				}
			}
		}
	case oldLive > 0 && availTotal > minAvail && !rel.SawFlap:
		// 2) Either the surge ceiling is reached (free a slot for the next new
		// instance), or newLive already reached baseline (shrink old revision).
		// The guard is exactly the maxUnavailable invariant; the SawFlap guard
		// keeps proven old capacity once a new replica proved unstable.
		if err := c.terminateOldestOld(ctx, instances, rel, tick, wt); err != nil {
			return err
		}
		acted = true
	default:
		// 3) Cannot add and cannot safely remove: wait for new readiness. This
		// is also the honest no-progress state for an impossible policy
		// (surge=0, unavailable=0), a full capacity fixture, or a frozen
		// scale-down after jitter.
		reason := "at policy ceiling"
		switch {
		case rel.SawFlap:
			reason = "old scale-down frozen after new readiness instability"
		case needNew:
			reason = "waiting for capacity or safe scale-down"
		}
		ev := c.mkEvent(tick, w.Name, rel, model.LevelInfo, model.EvWaitReady,
			"%s: live=%d maxLive=%d available=%d minAvailable=%d newLive=%d newAvailable=%d oldLive=%d; waiting for new instances to stabilize",
			reason, liveTotal, maxLive, availTotal, minAvail, newLive, newAvail, oldLive)
		if _, err := c.st.AppendEvent(ctx, ev); err != nil {
			return err
		}
		wt.Events = append(wt.Events, ev)
	}

	// Even when an action was taken, re-check completion from stored state.
	cur, err := c.st.ListInstances(ctx, w.Name)
	if err != nil {
		return err
	}
	var nAvail, oLive int
	for _, ins := range cur {
		if ins.Revision == rel.Revision {
			if ins.Available() {
				nAvail++
			}
		} else if ins.Live() {
			oLive++
		}
	}
	if nAvail >= baseline && oLive == 0 {
		return c.succeedRelease(ctx, w, rel, tick, wt)
	}

	if acted || newAvailChangedSinceLast(wt, rel) {
		rel.LastProgressTick = tick
	}
	return c.st.UpdateRelease(ctx, rel)
}

// newAvailChangedSinceLast is a conservative helper: the presence of an
// instance_ready event this tick is a definite availability gain.
func newAvailChangedSinceLast(wt *WorkloadTick, rel *model.Release) bool {
	if rel == nil {
		return false
	}
	for _, e := range wt.Events {
		if e.Type == model.EvInstanceReady && e.Revision == rel.Revision {
			return true
		}
	}
	return false
}

// tryStartNew starts one new-revision instance. started=true means a handle
// was created; blocked=true means the capacity fixture refused the start.
func (c *Controller) tryStartNew(ctx context.Context, w *model.Workload, rel *model.Release, tick int64, wt *WorkloadTick) (started, blocked bool, err error) {
	procID, serr := c.pm.Start(ctx, adapter.StartMeta{Workload: w.Name, Revision: rel.Revision, ReleaseID: rel.ID})
	switch {
	case serr == nil:
		if err := c.insertStartingInstance(ctx, w.Name, rel.Revision, rel.ID, procID, tick); err != nil {
			return false, false, err
		}
		ev := c.mkEvent(tick, w.Name, rel, model.LevelInfo, model.EvInstanceStart,
			"start requested for new instance on revision %s (proc=%s); created, NOT ready yet", rel.Revision, procID)
		ev.Revision = rel.Revision
		if _, err := c.st.AppendEvent(ctx, ev); err != nil {
			return false, false, err
		}
		wt.Events = append(wt.Events, ev)
		return true, false, nil
	case errors.Is(serr, adapter.ErrCapacity):
		return false, true, nil
	case errors.Is(serr, adapter.ErrStartRejected):
		// Hard fixture failure: record a failed instance occupying no slot.
		ins := &model.Instance{
			ID: genID("i"), Workload: w.Name, Revision: rel.Revision, ReleaseID: rel.ID,
			State: model.StateFailed, FailCategory: model.FailStartFailed,
			FailMessage: "process manager rejected start: " + serr.Error(),
			CreatedTick: tick, FailedTick: tick,
			CreatedAt: c.now(), UpdatedAt: c.now(),
		}
		if err := c.st.InsertInstance(ctx, ins); err != nil {
			return false, false, err
		}
		ev := c.mkEvent(tick, w.Name, rel, model.LevelFail, model.EvInstanceStartFailed,
			"new instance start rejected on revision %s: %s", rel.Revision, serr.Error())
		ev.InstanceID, ev.Revision, ev.Category = ins.ID, rel.Revision, string(model.FailStartFailed)
		if _, err := c.st.AppendEvent(ctx, ev); err != nil {
			return false, false, err
		}
		wt.Events = append(wt.Events, ev)
		// This is still a structural step (and a hard fact), so it is progress.
		rel.LastProgressTick = tick
		if err := c.st.UpdateRelease(ctx, rel); err != nil {
			return false, false, err
		}
		return false, false, nil
	default:
		return false, false, fmt.Errorf("start on %s/%s: %w", w.Name, rel.Revision, serr)
	}
}

func (c *Controller) insertStartingInstance(ctx context.Context, workload, revision, releaseID, procID string, tick int64) error {
	ins := &model.Instance{
		ID: genID("i"), Workload: workload, Revision: revision, ReleaseID: releaseID,
		ProcID: procID, State: model.StateStarting, CreatedTick: tick,
		CreatedAt: c.now(), UpdatedAt: c.now(),
	}
	return c.st.InsertInstance(ctx, ins)
}

// terminateOldestOld removes one old-revision instance, preferring a
// not-ready old instance (it contributes no availability) and otherwise the
// oldest one. Caller has already checked the availability invariant.
func (c *Controller) terminateOldestOld(ctx context.Context, instances []*model.Instance, rel *model.Release, tick int64, wt *WorkloadTick) error {
	var victim *model.Instance
	for _, ins := range instances {
		if ins.Revision == rel.Revision || !ins.Live() {
			continue
		}
		if victim == nil {
			victim = ins
			continue
		}
		// Prefer a not-ready old instance (removing it costs no availability);
		// among equal readiness prefer the oldest.
		curAvail := ins.Available()
		vicAvail := victim.Available()
		if curAvail != vicAvail {
			if !curAvail {
				victim = ins
			}
			continue
		}
		if ins.CreatedTick < victim.CreatedTick {
			victim = ins
		}
	}
	if victim == nil {
		return nil
	}
	if err := c.pm.Terminate(ctx, victim.ProcID); err != nil && !errors.Is(err, adapter.ProcGone) {
		return fmt.Errorf("terminate instance %s: %w", victim.ID, err)
	}
	victim.State = model.StateTerminated
	victim.TerminatedTick = tick
	victim.ProcID = ""
	victim.UpdatedAt = c.now()
	if err := c.st.UpdateInstance(ctx, victim); err != nil {
		return err
	}
	ev := c.mkEvent(tick, victim.Workload, rel, model.LevelInfo, model.EvInstanceTerminated,
		"old instance %s (revision %s) removed to free a slot for revision %s", victim.ID, victim.Revision, rel.Revision)
	ev.InstanceID, ev.Revision = victim.ID, victim.Revision
	if _, err := c.st.AppendEvent(ctx, ev); err != nil {
		return err
	}
	wt.Events = append(wt.Events, ev)
	return nil
}

func (c *Controller) blockedEvent(ctx context.Context, rel *model.Release, tick int64, wt *WorkloadTick, format string, args ...any) error {
	ev := c.mkEvent(tick, rel.Workload, rel, model.LevelWarn, model.EvBlockedCapacity, fmt.Sprintf(format, args...))
	ev.Category = string(model.FailInsufficientCapacity)
	ev.Certain = false // transient: capacity might free up on a later tick
	if _, err := c.st.AppendEvent(ctx, ev); err != nil {
		return err
	}
	wt.Events = append(wt.Events, ev)
	return nil
}

func (c *Controller) succeedRelease(ctx context.Context, w *model.Workload, rel *model.Release, tick int64, wt *WorkloadTick) error {
	rel.State = model.RelSucceeded
	rel.FinishedTick = tick
	rel.LastProgressTick = tick
	if err := c.st.UpdateRelease(ctx, rel); err != nil {
		return err
	}
	if err := c.st.SetWorkloadCurrent(ctx, w.Name, rel.Revision, rel.ID); err != nil {
		return err
	}
	ev := c.mkEvent(tick, w.Name, rel, model.LevelInfo, model.EvRolloutSucceeded,
		"release %s complete: workload %s now fully on revision %s", rel.ID, w.Name, rel.Revision)
	if _, err := c.st.AppendEvent(ctx, ev); err != nil {
		return err
	}
	wt.Events = append(wt.Events, ev)
	return nil
}

func (c *Controller) failRelease(ctx context.Context, w *model.Workload, rel *model.Release, tick int64, wt *WorkloadTick) error {
	rel.State = model.RelFailed
	rel.FinishedTick = tick
	if err := c.st.UpdateRelease(ctx, rel); err != nil {
		return err
	}
	// NOTE: workload current revision/current release are intentionally NOT
	// changed: the previous revision keeps serving. History row is retained.
	ev := c.mkEvent(tick, w.Name, rel, model.LevelFail, model.EvRolloutFailed,
		"release %s FAILED at revision %s [%s]: %s — previous revision %s keeps serving",
		rel.ID, rel.Revision, rel.FailureCategory, rel.FailMessage, w.CurrentRevision)
	ev.Category = string(rel.FailureCategory)
	if _, err := c.st.AppendEvent(ctx, ev); err != nil {
		return err
	}
	wt.Events = append(wt.Events, ev)
	c.log.Error("release failed",
		"request_id", rel.RequestID, "release", rel.ID, "workload", w.Name,
		"revision", rel.Revision, "category", rel.FailureCategory, "tick", tick, "reason", rel.FailMessage)
	return nil
}

// ---------------------------------------------------------------- helpers

// isNewOf reports whether an instance belongs to the in-flight release's
// revision. Membership is by revision so that rollback releases (new release
// row, old revision) classify instances correctly.
func isNewOf(ins *model.Instance, rel *model.Release) bool {
	return rel != nil && ins.Revision == rel.Revision
}

func fillCounts(wt *WorkloadTick, instances []*model.Instance, rel *model.Release) {
	wt.Live, wt.Available, wt.NewLive, wt.NewAvailable, wt.OldLive, wt.OldAvailable = 0, 0, 0, 0, 0, 0
	for _, ins := range instances {
		if ins.Live() {
			wt.Live++
		}
		if ins.Available() {
			wt.Available++
		}
		if isNewOf(ins, rel) {
			if ins.Live() {
				wt.NewLive++
			}
			if ins.Available() {
				wt.NewAvailable++
			}
		} else {
			if ins.Live() {
				wt.OldLive++
			}
			if ins.Available() {
				wt.OldAvailable++
			}
		}
	}
}

func (c *Controller) mkEvent(tick int64, workload string, rel *model.Release, level model.EventLevel, typ, format string, args ...any) *model.Event {
	e := &model.Event{
		Tick: tick, TS: c.now(), Workload: workload,
		Level: level, Type: typ, Certain: true, Message: fmt.Sprintf(format, args...),
	}
	if rel != nil {
		e.ReleaseID = rel.ID
		e.RequestID = rel.RequestID
	}
	return e
}

func orDefault(s, d string) string {
	if s == "" {
		return d
	}
	return s
}
