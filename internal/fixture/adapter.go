// Package fixture provides local synthetic adapters: in real deployments these
// calls would hit an API server (list pods, observe readiness, evict a pod).
// Here every "external participant" is a deterministic, in-process, scripted
// source so the whole system is reproducible with no production account or
// real business data.
package fixture

import (
	"context"
	"sync"
	"time"

	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/store"
)

// Script is the programmed behaviour of one synthetic instance.
type Script struct {
	Instance domain.Instance
	// ReadyTimeline maps a tick index to readiness. Unknown ticks are
	// represented by entries missing from the map AND Observed=false: an
	// adapter simply does not emit an observation then.
	Ready    map[int]bool
	Observed map[int]bool
	// FailAtTick >= 0 means an involuntary failure happens at that tick once.
	FailAtTick int
	FailReason string
}

// Adapter is a scripted readiness/failure source plus a scripted eviction
// actuator. It holds no knowledge of budgets — it only reports facts and
// performs evictions when told to.
type Adapter struct {
	st      *store.Store
	mu      sync.Mutex
	scripts map[string]*Script
	tick    int
	clock   func() time.Time
	failed  map[string]bool
	// Evicted records successful eviction actuations keyed by approval id.
	Evicted map[string]EvictionAct
}

// EvictionAct records that the synthetic actuator removed an instance.
type EvictionAct struct {
	ApprovalID string
	InstanceID string
	At         time.Time
}

// NewAdapter builds an adapter over the given store.
func NewAdapter(st *store.Store, clock func() time.Time) *Adapter {
	if clock == nil {
		clock = time.Now
	}
	return &Adapter{
		st:      st,
		scripts: map[string]*Script{},
		clock:   clock,
		failed:  map[string]bool{},
		Evicted: map[string]EvictionAct{},
	}
}

// LoadScript registers an instance's behaviour script.
func (a *Adapter) LoadScript(s Script) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.scripts[s.Instance.ID] = &s
}

// Tick advances the synthetic observation clock by one and emits facts.
func (a *Adapter) Tick(ctx context.Context) (int, error) {
	a.mu.Lock()
	a.tick++
	tick := a.tick
	scripts := make([]*Script, 0, len(a.scripts))
	for _, s := range a.scripts {
		scripts = append(scripts, s)
	}
	a.mu.Unlock()

	now := a.clock()
	for _, s := range scripts {
		// Involuntary failure first — it is independent of readiness reporting.
		if s.FailAtTick >= 0 && tick >= s.FailAtTick && !a.failed[s.Instance.ID] {
			a.mu.Lock()
			if !a.failed[s.Instance.ID] {
				a.failed[s.Instance.ID] = true
			}
			a.mu.Unlock()
			if err := a.st.RecordFailure(ctx, domain.Failure{
				InstanceID: s.Instance.ID,
				Reason:     s.FailReason,
				// adapter doesn't know the epoch; coordinator-driven scenarios
				// pass it explicitly via RecordFailureAtEpoch below.
				At:     now,
				Source: "synthetic-node-monitor",
			}); err != nil {
				return tick, err
			}
		}
		observed := true
		if s.Observed != nil {
			observed = s.Observed[tick]
		}
		if !observed {
			continue
		}
		ready := false
		if s.Ready != nil {
			ready = s.Ready[tick]
		}
		if err := a.st.RecordObservation(ctx, s.Instance, domain.Observation{
			InstanceID: s.Instance.ID,
			Ready:      ready,
			At:         now,
			Source:     "synthetic-kubelet-probe",
		}); err != nil {
			return tick, err
		}
	}
	return tick, nil
}

// EmitObservation records one readiness fact at an explicit epoch. Scenarios
// use this after they know the group's current selector epoch.
func (a *Adapter) EmitObservation(ctx context.Context, inst domain.Instance, ready bool, epoch int64) error {
	return a.st.RecordObservation(ctx, inst, domain.Observation{
		InstanceID: inst.ID,
		Ready:      ready,
		Epoch:      epoch,
		At:         a.clock(),
		Source:     "synthetic-kubelet-probe",
	})
}

// EmitFailure records one involuntary failure at an explicit epoch.
func (a *Adapter) EmitFailure(ctx context.Context, instanceID, reason string, epoch int64) error {
	a.mu.Lock()
	a.failed[instanceID] = true
	a.mu.Unlock()
	return a.st.RecordFailure(ctx, domain.Failure{
		InstanceID: instanceID,
		Reason:     reason,
		Epoch:      epoch,
		At:         a.clock(),
		Source:     "synthetic-node-monitor",
	})
}

// Actuate simulates performing an approved eviction. It deliberately models a
// pause: when Pause is set the actuator holds the approval without completing
// it (used by the "approved, then stalled" scenario).
func (a *Adapter) Actuate(approvalID, instanceID string, at time.Time) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.Evicted[approvalID] = EvictionAct{
		ApprovalID: approvalID,
		InstanceID: instanceID,
		At:         at,
	}
}

// WasActuated reports whether an approved eviction was physically performed.
func (a *Adapter) WasActuated(approvalID string) (EvictionAct, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	act, ok := a.Evicted[approvalID]
	return act, ok
}
