package core

import (
	"fmt"
	"sort"

	"lifecycle.local/v1/internal/model"
)

// round executes one fixed-point iteration. Helpers mutate the in-memory
// index in the same way the generated actions will mutate storage, so
// later rounds within the tick observe marks/deletes immediately.
func (p *Planner) round() {
	uids := sortedResourceUIDs(p.idx)

	// Phase A: Orphan policy — strip this owner's uid from dependents
	// before the owner disappears. Must precede cascade evaluation so
	// the ordering "orphan strip vs block flag" is explicit and testable.
	for _, uid := range uids {
		r := p.idx.ByUID[uid]
		if r == nil {
			continue
		}
		if r.IsDeleting() && r.Policy() == model.PolicyOrphan &&
			r.HasFinalizer(model.FinalizerOrphanDependents) {
			p.processOrphan(r)
		}
	}

	// Phase B: cascade GC evaluation for live, not-yet-deleting
	// resources. Resolving references BY UID is what makes same-name
	// recreate safe: an uidMismatch ref is dropped, never re-pointed.
	for _, uid := range uids {
		r := p.idx.ByUID[uid]
		if r == nil || r.IsDeleting() {
			continue
		}
		p.evaluateCascade(r)
	}

	// Phase C: foreground blocking graph — diagnose cycles instead of
	// waiting on them, then decide which deleting resources are blocked.
	g := BuildBlockingGraph(p.idx)
	cycles := g.FindCycles()
	if len(cycles) > 0 {
		p.handleCycles(cycles)
		g = BuildBlockingGraph(p.idx)
	}
	blocked := p.blockedForegroundSet(g)

	// Phase D: deleting resources — finalizers then physical delete.
	for _, uid := range uids {
		r := p.idx.ByUID[uid]
		if r == nil || !r.IsDeleting() {
			continue
		}
		p.processDeleting(r, blocked[uid])
	}
}

func sortedResourceUIDs(idx *LiveIndex) []string {
	out := make([]string, 0, len(idx.ByUID))
	for u := range idx.ByUID {
		out = append(out, u)
	}
	sort.Strings(out)
	return out
}

// processOrphan removes every still-live dependent's reference to r and
// then drops the orphan controller finalizer, which lets r proceed to
// physical deletion within the same tick.
func (p *Planner) processOrphan(r *model.Resource) {
	changed := false
	for _, d := range sortedResourceUIDs(p.idx) {
		dep := p.idx.ByUID[d]
		if dep == nil {
			continue
		}
		for i, ref := range dep.OwnerRefs {
			if ref.UID != r.UID {
				continue
			}
			ev := p.evt(model.EvOrphanRefStripped, dep)
			ev.OtherUID = r.UID
			ev.OtherName = r.QualifiedName()
			ev.Policy = model.PolicyOrphan
			ev.Message = fmt.Sprintf("Orphan delete of owner %s: reference removed from dependent", r.QualifiedName())
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActRemoveOwnerRef, ResourceUID: dep.UID, OwnerUID: r.UID,
				Event: ev,
			})
			dep.OwnerRefs = append(dep.OwnerRefs[:i], dep.OwnerRefs[i+1:]...)
			changed = true
			break
		}
	}
	if changed {
		cond := p.trueCondition(model.ConditionOrphaning, model.ReasonOrphanStripping,
			fmt.Sprintf("dependents detached before orphan delete (owner %s)", r.QualifiedName()), r.Generation)
		p.plan.Actions = append(p.plan.Actions, Action{
			Kind: ActSetCondition, ResourceUID: r.UID, Condition: cond,
		})
	}
	p.removeFinalizerWithAction(r, model.FinalizerOrphanDependents,
		"orphan dependents detached; orphan finalizer released")
}

// evaluateCascade resolves r's references and marks r for deletion when
// every owner is gone / cascade-deleting. Multiple live or
// non-cascade-deleting owners keep r alive — that is the "diamond"
// guarantee that collection only hits resources which lost ALL valid
// owners.
func (p *Planner) evaluateCascade(r *model.Resource) {
	res := p.resolveRefs(r)

	// 1) Drop unresolvable references with a concrete diagnostic each.
	var liveRefs []RefResolution
	for _, rr := range res {
		switch rr.Status {
		case RefResolved:
			liveRefs = append(liveRefs, rr)
		case RefUIDMismatch:
			sameName, _ := p.idx.LookupName(rr.Ref.Namespace, rr.Ref.Name)
			ev := p.evt(model.EvStaleRefDropped, r)
			ev.OtherUID = rr.Ref.UID
			ev.OtherName = rr.Ref.Namespace + "/" + rr.Ref.Name
			ev.Reason = string(RefUIDMismatch)
			ev.Message = fmt.Sprintf(
				"ownerRef pinned old incarnation uid %s but live object at that name is uid %s; dropping stale ref without re-parenting",
				rr.Ref.UID, sameName.UID)
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActRemoveOwnerRef, ResourceUID: r.UID, OwnerUID: rr.Ref.UID, Event: ev,
			})
			r.removeOwnerRef(rr.Ref.UID)
		case RefOwnerGone:
			ev := p.evt(model.EvStaleRefDropped, r)
			ev.OtherUID = rr.Ref.UID
			ev.OtherName = rr.Ref.Namespace + "/" + rr.Ref.Name
			ev.Reason = string(RefOwnerGone)
			ev.Message = "ownerRef target uid does not exist and no tombstone; dropping dangling ref (resource retained: no explicit cascade intent)"
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActRemoveOwnerRef, ResourceUID: r.UID, OwnerUID: rr.Ref.UID, Event: ev,
			})
			r.removeOwnerRef(rr.Ref.UID)
		case RefOrphanTomb:
			ev := p.evt(model.EvStaleRefDropped, r)
			ev.OtherUID = rr.Ref.UID
			ev.OtherName = rr.Ref.Namespace + "/" + rr.Ref.Name
			ev.Reason = string(RefOrphanTomb)
			ev.Policy = model.PolicyOrphan
			ev.Message = "owner was deleted with Orphan policy; reference removed and dependent retained"
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActRemoveOwnerRef, ResourceUID: r.UID, OwnerUID: rr.Ref.UID, Event: ev,
			})
			r.removeOwnerRef(rr.Ref.UID)
		case RefCascadeTomb:
			// Owner already physically gone under a cascade policy; the
			// reference is removed but the resource is only collected if
			// no other valid owner remains (handled below).
			ev := p.evt(model.EvStaleRefDropped, r)
			ev.OtherUID = rr.Ref.UID
			ev.OtherName = rr.Ref.Namespace + "/" + rr.Ref.Name
			ev.Reason = string(RefCascadeTomb)
			ev.Policy = rr.Tomb.Policy
			ev.Message = "cascade-deleted owner tombstone matched; ref removed"
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActRemoveOwnerRef, ResourceUID: r.UID, OwnerUID: rr.Ref.UID, Event: ev,
			})
			r.removeOwnerRef(rr.Ref.UID)
		}
	}

	// 2) Decide whether remaining (live) owners still protect r.
	//
	// Rules, in order:
	//   - zero refs originally: standalone resource, never auto-collected;
	//   - any orphan tombstone among the (already removed) refs: that
	//     owner deliberately detached r — r survives even if other
	//     owners later disappear;
	//   - any live NON-deleting owner: protected;
	//   - a live owner deleted under Orphan: protected (phase A strips
	//     the ref on a later round);
	//   - all remaining refs come from deleting owners whose cascade
	//     reaches r: mark r with the strongest inherited policy
	//     (Foreground > Background);
	//   - otherwise the only losses were cascade tombstones / dangling
	//     refs with no cascade intent at all (OwnerGone is treated as a
	//     cascade loss because the owner vanished without detaching).
	if len(res) == 0 {
		return
	}

	var orphanTomb, cascadeTomb bool
	for _, rr := range res {
		switch rr.Status {
		case RefOrphanTomb:
			orphanTomb = true
		case RefCascadeTomb:
			cascadeTomb = true
		}
	}
	if orphanTomb {
		// Explicit Orphan intent wins over every cascade: the dependent
		// was intentionally detached and must remain.
		return
	}

	if len(liveRefs) > 0 {
		protected := false
		inheritedFG := false
		inheritedBG := false
		for _, rr := range liveRefs {
			o := rr.Live
			if !o.IsDeleting() {
				protected = true // a surviving co-owner keeps the diamond child
				break
			}
			switch o.Policy() {
			case model.PolicyOrphan:
				protected = true // phase A detaches the ref; never collected
			case model.PolicyForeground:
				if rr.BlockFG {
					inheritedFG = true
				} else {
					inheritedBG = true
				}
			case model.PolicyBackground:
				inheritedBG = true
			}
		}
		if protected {
			return
		}
		if inheritedFG || inheritedBG {
			policy := model.PolicyBackground
			if inheritedFG {
				policy = model.PolicyForeground
			}
			p.markDeleting(r, policy, fmt.Sprintf(
				"all %d remaining owner(s) deleting; inherited %s cascade", len(liveRefs), policy))
		}
		// Live refs with inherited==false can't occur; if it ever did,
		// doing nothing is the safe choice (no silent delete).
		return
	}

	// No live refs. Surviving references were removed this round. GC
	// only collects when a cascade loss occurred (a cascade tombstone or
	// an owner that vanished without detaching); a graph that consists
	// solely of orphan tombstones was handled above.
	if cascadeTomb || ownerGone {
		p.markDeleting(r, model.PolicyBackground,
			"no valid owner remains after ref resolution; background garbage collection")
	}
}

// blockedForegroundSet returns foreground-deleting resources that must
// still wait for at least one blocking dependent (cycle-forced nodes
// excluded — their wait was released deterministically).
func (p *Planner) blockedForegroundSet(g *BlockingGraph) map[string]bool {
	out := map[string]bool{}
	for u, blockers := range g.Out {
		if p.broken[u] {
			continue
		}
		if len(blockers) > 0 {
			out[u] = true
		}
	}
	return out
}

func (p *Planner) handleCycles(cycles []Cycle) {
	broken := BreakUID(cycles)
	now := p.now
	for _, c := range cycles {
		ev := &model.GCEvent{
			RunID: p.runID, Tick: p.tick, Type: model.EvCycleDetected,
			UID: c.UIDs[0], OtherUID: broken,
			Reason:     model.ReasonCycleDetected,
			Message:    fmt.Sprintf("illegal foreground deletion blocking cycle: %v", c.UIDs),
			OccurredAt: now,
		}
		cm := &model.CycleMark{
			Tick: p.tick, Cycle: append([]string{}, c.UIDs...),
			BrokenUID: broken, Reason: model.ReasonCycleDetected, NotedAt: now,
		}
		p.plan.Actions = append(p.plan.Actions, Action{
			Kind: ActInsertCycleMark, Event: ev, Cycle: cm,
		})
	}
	p.broken[broken] = true
	r := p.idx.ByUID[broken]
	if r != nil && isForegroundDeleting(r) {
		ev := p.evt(model.EvCycleBroken, r)
		ev.OtherUID = broken
		ev.Reason = model.ReasonCycleDetected
		ev.Message = fmt.Sprintf("cycle broken at deterministic node %s (smallest uid); foreground wait released", broken)
		cond := p.trueCondition(model.ConditionCycle, model.ReasonCycleDetected,
			"foreground blocking cycle detected and broken at smallest uid "+broken, r.Generation)
		p.plan.Actions = append(p.plan.Actions, Action{
			Kind: ActRemoveFinalizer, ResourceUID: broken,
			Finalizer: model.FinalizerDeletionCohort, Event: ev, Condition: cond,
		})
		r.removeFinalizer(model.FinalizerDeletionCohort)
	}
}

// processDeleting drives one deleting resource: emit/update its
// Terminating condition, attempt one user finalizer per tick, honor the
// foreground block, release the foreground cohort when the wait ends,
// and physically delete when nothing remains.
func (p *Planner) processDeleting(r *model.Resource, blocked bool) {
	// Terminating condition reflects the current wait state. Re-set
	// every round is cheap, but the noisy per-blocker DeletionBlocked
	// events are deduplicated within the tick so a stable blocked state
	// cannot prevent fixed-point convergence.
	waitMsg := "deletion in progress"
	waitReason := ""
	if blocked {
		waitMsg = fmt.Sprintf("foreground deletion waiting for %d blocking dependent(s)", len(p.directBlockers(r)))
		waitReason = model.ReasonDeletionBlocked
	}
	if p.note("term-cond:" + r.UID + ":" + waitReason) {
		cond := p.trueCondition(model.ConditionTerminating, waitReason, waitMsg, r.Generation)
		p.plan.Actions = append(p.plan.Actions, Action{
			Kind: ActSetCondition, ResourceUID: r.UID, Condition: cond,
		})
	}

	if blocked {
		for _, b := range p.directBlockers(r) {
			if !p.note("blocked:" + r.UID + ":" + b.UID) {
				continue
			}
			ev := p.evt(model.EvDeletionBlocked, r)
			ev.OtherUID = b.UID
			ev.OtherName = b.QualifiedName()
			ev.Policy = model.PolicyForeground
			ev.Reason = model.ReasonDeletionBlocked
			ev.Message = "blockOwnerDeletion dependent still present; foreground owner retained"
			p.plan.Actions = append(p.plan.Actions, Action{Kind: ActEmitEvent, Event: ev})
		}
		return
	}

	// Foreground cohort guard: no blocker remains (either every blocking
	// dependent was collected or a cycle was diagnosed and broken here),
	// so the wait is over — release the cohort finalizer. When a cycle
	// break already removed it this tick the HasFinalizer test is false.
	if r.Policy() == model.PolicyForeground && r.HasFinalizer(model.FinalizerDeletionCohort) {
		p.removeFinalizerWithAction(r, model.FinalizerDeletionCohort,
			"all blocking dependents gone; foreground cohort released")
		return // delete runs in a subsequent round
	}

	// One user finalizer attempt per resource per tick.
	if key, ok := p.nextUserFinalizer(r); ok {
		if p.collect {
			if !p.attempted[r.UID] {
				p.attempted[r.UID] = true
				p.calls = append(p.calls, FinalizerCall{ResourceUID: r.UID, Key: key})
			}
			return
		}
		if p.attempted[r.UID] {
			return // outcome already incorporated this tick
		}
		p.attempted[r.UID] = true
		outcome, hasOutcome := p.outcomes[OutcomeKey(r.UID, key)]

		evCalled := p.evt(model.EvFinalizerCalled, r)
		evCalled.Finalizer = key
		evCalled.Message = "invoking user finalizer"
		p.plan.Actions = append(p.plan.Actions, Action{Kind: ActEmitEvent, Event: evCalled})

		if !hasOutcome {
			// Defensive: reconcile must supply outcomes for every call.
			// It is reported as a concrete failure category, never as
			// success.
			outcome = FinalizerOutcome{Failed: true,
				Err: "internal: missing finalizer outcome (handler not executed)"}
		}
		switch {
		case outcome.Panicked:
			ev := p.evt(model.EvFinalizerFailed, r)
			ev.Finalizer = key
			ev.Reason = model.ReasonFinalizerPanic
			ev.Message = "finalizer handler panicked (recovered): " + outcome.Err
			fcond := p.trueCondition(model.ConditionFinalizerFailure,
				model.ReasonFinalizerPanic, ev.Message, r.Generation)
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActSetCondition, ResourceUID: r.UID, Event: ev, Condition: fcond,
			})
			return
		case outcome.Failed:
			ev := p.evt(model.EvFinalizerFailed, r)
			ev.Finalizer = key
			ev.Reason = model.ReasonFinalizerFailed
			ev.Message = "finalizer failed and will be retried: " + outcome.Err
			fcond := p.trueCondition(model.ConditionFinalizerFailure,
				model.ReasonFinalizerFailed, ev.Message, r.Generation)
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActSetCondition, ResourceUID: r.UID, Event: ev, Condition: fcond,
			})
			return
		default:
			ev := p.evt(model.EvFinalizerRecovered, r)
			ev.Finalizer = key
			ev.Message = "finalizer completed"
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActRemoveFinalizer, ResourceUID: r.UID, Finalizer: key, Event: ev,
			})
			p.plan.Actions = append(p.plan.Actions, Action{
				Kind: ActDeleteCondition, ResourceUID: r.UID,
				ConditionType: model.ConditionFinalizerFailure,
			})
			r.removeFinalizer(key)
		}
		return // next round continues with the next finalizer or delete
	}

	// No user finalizers remain. The orphan finalizer is handled in
	// phase A; the foreground cohort was released above or via a cycle
	// break. Any other surviving reserved finalizer is an invariant
	// violation and reported, not swallowed.
	for _, f := range r.Finalizers {
		if f != model.FinalizerOrphanDependents {
			p.emitInvariant("finalizer %q could not be scheduled on %s", f, r.QualifiedName())
			return
		}
	}

	p.physicalDelete(r)
}

// directBlockers returns the foreground-blocking dependents of r that
// still exist in the index.
func (p *Planner) directBlockers(r *model.Resource) []*model.Resource {
	var out []*model.Resource
	for _, d := range p.idx.ByUID {
		for _, ref := range d.OwnerRefs {
			if ref.UID == r.UID && ref.BlockOwnerDeletion && isForegroundDeleting(d) {
				out = append(out, d)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].UID < out[j].UID })
	return out
}

func (p *Planner) nextUserFinalizer(r *model.Resource) (string, bool) {
	for _, f := range r.Finalizers {
		if f == model.FinalizerDeletionCohort || f == model.FinalizerOrphanDependents {
			continue
		}
		return f, true
	}
	return "", false
}

func (p *Planner) markDeleting(r *model.Resource, policy, why string) {
	now := p.now
	r.DeletionTimestamp = &now
	r.DeletionPolicy = policy
	actions := []Action{{
		Kind: ActMarkDeleting, ResourceUID: r.UID, Policy: policy,
	}}
	switch policy {
	case model.PolicyForeground:
		actions = append(actions, Action{
			Kind: ActAddFinalizer, ResourceUID: r.UID,
			Finalizer: model.FinalizerDeletionCohort,
		})
		r.Finalizers = append(r.Finalizers, model.FinalizerDeletionCohort)
	case model.PolicyOrphan:
		actions = append(actions, Action{
			Kind: ActAddFinalizer, ResourceUID: r.UID,
			Finalizer: model.FinalizerOrphanDependents,
		})
		r.Finalizers = append(r.Finalizers, model.FinalizerOrphanDependents)
	}
	ev := p.evt(model.EvDeletionMarked, r)
	ev.Policy = policy
	ev.Message = why
	actions[0].Event = ev
	p.plan.Actions = append(p.plan.Actions, actions...)
}

func (p *Planner) note(key string) bool {
	if p.noted[key] {
		return false
	}
	p.noted[key] = true
	return true
}

func (p *Planner) removeFinalizerWithAction(r *model.Resource, key, why string) {
	r.removeFinalizer(key)
	p.plan.Actions = append(p.plan.Actions, Action{
		Kind: ActRemoveFinalizer, ResourceUID: r.UID, Finalizer: key,
	})
	if why != "" {
		ev := p.evt(model.EvFinalizerRecovered, r)
		ev.Finalizer = key
		ev.Message = why
		p.plan.Actions = append(p.plan.Actions, Action{Kind: ActEmitEvent, Event: ev})
	}
}

func (p *Planner) physicalDelete(r *model.Resource) {
	ev := p.evt(model.EvResourceDeleted, r)
	ev.Policy = r.Policy()
	ev.Message = "all finalizers released and no blocking dependents; physically deleting"
	p.plan.Actions = append(p.plan.Actions, Action{
		Kind: ActPhysicalDelete, ResourceUID: r.UID,
		Namespace: r.Namespace, Name: r.Name, Policy: r.Policy(), Event: ev,
	}, Action{
		Kind: ActInsertTomb,
		Tomb: &model.DeletedOwner{
			UID: r.UID, Namespace: r.Namespace, Name: r.Name,
			Policy: r.Policy(), DeletedAt: p.now, Tick: p.tick,
		},
	})
	delete(p.idx.ByUID, r.UID)
	delete(p.idx.ByName, NameKey(r.Namespace, r.Name))
}

func (r *model.Resource) removeOwnerRef(uid string) {
	out := r.OwnerRefs[:0]
	for _, ref := range r.OwnerRefs {
		if ref.UID != uid {
			out = append(out, ref)
		}
	}
	r.OwnerRefs = out
}

func (r *model.Resource) removeFinalizer(key string) {
	out := r.Finalizers[:0]
	for _, f := range r.Finalizers {
		if f != key {
			out = append(out, f)
		}
	}
	r.Finalizers = out
}
