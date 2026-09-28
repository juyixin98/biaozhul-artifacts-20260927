package core

import (
	"fmt"
	"sort"
	"time"

	"lifecycle.local/v1/internal/model"
)

// FinalizerOutcome is the result of one user-finalizer attempt. The
// planner never invokes finalizers itself; the reconcile layer supplies
// a callback so that I/O (HTTP adapters, counters) stays out of core.
type FinalizerOutcome struct {
	// Failed means the finalizer did not complete and must be retried
	// on a later tick.
	Failed bool
	// Panicked reports the handler panicked (still treated as a failed,
	// retryable finalizer with its own reason).
	Panicked bool
	// Err is the diagnostic message for Failed/Panicked.
	Err string
}

// FinalizerCall is one (resource uid, finalizer key) pair the planner
// wants attempted this tick. At most one user finalizer per resource is
// attempted per tick.
type FinalizerCall struct {
	ResourceUID string
	Key         string
}

// RefResolution records how one owner reference resolved.
type RefResolution struct {
	Ref      model.OwnerRef
	Status   RefStatus
	Live     *model.Resource // when Status == RefResolved
	Tomb     *model.DeletedOwner
	BlockFG  bool // ref blocks and a foreground deletion is in flight on the owner
}

// RefStatus values.
type RefStatus string

const (
	// RefResolved: pinned owner incarnation is live.
	RefResolved RefStatus = "resolved"
	// RefUIDMismatch: a live object exists at the (ns,name) tuple but
	// its uid differs from the pinned uid (name reuse after recreate).
	RefUIDMismatch RefStatus = "uidMismatch"
	// RefOwnerGone: nothing exists at the tuple and no tombstone for
	// the pinned uid.
	RefOwnerGone RefStatus = "ownerGone"
	// RefOrphanTomb: pinned owner was physically deleted under Orphan.
	RefOrphanTomb RefStatus = "orphanTomb"
	// RefCascadeTomb: pinned owner was physically deleted under
	// Foreground/Background cascade.
	RefCascadeTomb RefStatus = "cascadeTomb"
)

// Action kinds carried by the plan and applied by the reconcile layer.
const (
	ActMarkDeleting     = "markDeleting"
	ActAddFinalizer     = "addFinalizer"
	ActRemoveFinalizer  = "removeFinalizer"
	ActRemoveOwnerRef   = "removeOwnerRef"
	ActPhysicalDelete   = "physicalDelete"
	ActInsertTomb       = "insertTomb"
	ActSetCondition     = "setCondition"
	ActDeleteCondition  = "deleteCondition"
	ActCallFinalizer    = "callFinalizer"
	ActEmitEvent        = "emitEvent"
	ActInsertCycleMark  = "insertCycleMark"
)

// Action is one planned mutation. Mutations carry exactly enough data
// for storage application; the reconcile layer applies them in order.
type Action struct {
	Kind         string
	ResourceUID  string
	Namespace    string
	Name         string
	Policy       string
	Finalizer    string
	OwnerUID     string
	Condition    *model.Condition
	ConditionType string
	Event        *model.GCEvent
	Tomb         *model.DeletedOwner
	Cycle        *model.CycleMark
}

// Plan is the output of one tick.
type Plan struct {
	Tick    int64
	RunID   string
	Actions []Action
	// Empty reports that no state changed (caller may idle).
	Empty bool
}

// Planner bundles the immutable inputs of one tick.
//
// The reconcile layer runs the planner twice per tick:
//
//  1. pass 1 (collect=true): rounds run read-only except for collecting
//     the set of FinalizerCalls that must be attempted;
//  2. the layer invokes the (I/O capable) finalizer handlers;
//  3. pass 2 (collect=false, outcomes supplied): rounds produce the
//     complete action list, including the consequences of the
//     finalizer outcomes.
//
// Keeping finalizer I/O outside the fixed-point loop guarantees that a
// handler can never be invoked an unbounded number of times in one tick.
type Planner struct {
	runID     string
	tick      int64
	now       time.Time
	idx       *LiveIndex
	tombs     map[string]*model.DeletedOwner
	outcomes  map[string]FinalizerOutcome // key: uid + "\x00" + finalizer
	collect   bool
	calls     []FinalizerCall
	attempted map[string]bool
	noted     map[string]bool // per-tick dedup of noisy conditions/events
	plan      *Plan
	broken    map[string]bool
}

// OutcomeKey is the map key for a finalizer result.
func OutcomeKey(uid, finalizer string) string { return uid + "\x00" + finalizer }

// NewPlanner builds a per-tick planner. When collect is true the planner
// gathers FinalizerCalls instead of applying finalizer consequences;
// outcomes must be nil in that pass.
func NewPlanner(runID string, tick int64, now time.Time,
	all []*model.Resource, tombs []model.DeletedOwner,
	collect bool, outcomes map[string]FinalizerOutcome) *Planner {
	p := &Planner{
		runID:     runID,
		tick:      tick,
		now:       now,
		idx:       NewLiveIndex(all),
		tombs:     map[string]*model.DeletedOwner{},
		outcomes:  outcomes,
		collect:   collect,
		attempted: map[string]bool{},
		noted:     map[string]bool{},
		plan:      &Plan{Tick: tick, RunID: runID},
		broken:    map[string]bool{},
	}
	for i := range tombs {
		t := tombs[i]
		p.tombs[t.UID] = &t
	}
	return p
}

// Calls returns the finalizer attempts gathered during the collect pass.
func (p *Planner) Calls() []FinalizerCall { return p.calls }

// Run executes the fixed-point rounds and returns the plan.
func (p *Planner) Run() *Plan {
	const maxRounds = 2 * 1024
	for round := 0; round < maxRounds; round++ {
		before := len(p.plan.Actions)
		p.round()
		if len(p.plan.Actions) == before {
			break
		}
		if round == maxRounds-1 {
			p.emitInvariant("planner fixed-point did not converge after %d rounds", maxRounds)
		}
	}
	p.plan.Empty = len(p.plan.Actions) == 0 && len(p.calls) == 0
	return p.plan
}

func (p *Planner) emitInvariant(format string, args ...any) {
	ev := &model.GCEvent{
		RunID:      p.runID,
		Tick:       p.tick,
		Type:       model.EvInvariantBroken,
		Reason:     model.ReasonInvariantBroken,
		Message:    fmt.Sprintf(format, args...),
		OccurredAt: p.now,
	}
	p.plan.Actions = append(p.plan.Actions, Action{Kind: ActEmitEvent, Event: ev,
		Condition: p.trueCondition(model.ConditionTerminating, model.ReasonInvariantBroken, ev.Message, 0)})
}

func (p *Planner) trueCondition(typ, reason, msg string, gen int64) *model.Condition {
	return &model.Condition{
		Type: typ, Status: model.ConditionTrue, Reason: reason,
		Message: msg, ObservedGeneration: gen, LastTransition: p.now,
	}
}

func (p *Planner) evt(evType string, r *model.Resource) *model.GCEvent {
	return &model.GCEvent{
		RunID: p.runID, Tick: p.tick, Type: evType,
		Namespace: r.Namespace, Name: r.Name, UID: r.UID,
		OccurredAt: p.now,
	}
}

// resolveRefs classifies every reference on r against the live index and
// tombstones.
func (p *Planner) resolveRefs(r *model.Resource) []RefResolution {
	out := make([]RefResolution, 0, len(r.OwnerRefs))
	for _, ref := range r.OwnerRefs {
		rr := RefResolution{Ref: ref}
		if live, ok := p.idx.ByUID[ref.UID]; ok {
			rr.Status = RefResolved
			rr.Live = live
			rr.BlockFG = ref.BlockOwnerDeletion && isForegroundDeleting(live)
			out = append(out, rr)
			continue
		}
		// pinned incarnation not live.
		if sameName, ok := p.idx.LookupName(ref.Namespace, ref.Name); ok && sameName.UID != ref.UID {
			rr.Status = RefUIDMismatch
			out = append(out, rr)
			continue
		}
		if tomb, ok := p.tombs[ref.UID]; ok {
			if tomb.Policy == model.PolicyOrphan {
				rr.Status = RefOrphanTomb
			} else {
				rr.Status = RefCascadeTomb
			}
			rr.Tomb = tomb
			out = append(out, rr)
			continue
		}
		rr.Status = RefOwnerGone
		out = append(out, rr)
	}
	return out
}
