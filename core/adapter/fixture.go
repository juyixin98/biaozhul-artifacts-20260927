// Package adapter contains the local synthetic fixture the controller is
// allowed to drive: an in-memory replica set with stable instance identities,
// a metric registry for per-instance reports, explicit fault-injection hooks
// for the failure-category tests, and no external participants whatsoever.
package adapter

import (
	"fmt"
	"sort"
	"sync"
	"time"

	"replicactl/core/model"
)

// LocalFixture is the only workload the algorithm ever touches. It implements
// controller.Fleet and controller.MetricSource.
type LocalFixture struct {
	mu sync.Mutex

	cfg model.Config
	// desired/current replica counts; instance slots are stable identities
	// (instance-001 ...), retained across a scale-to-zero round trip.
	replicas  int
	maxSlots  int
	instances []string

	// samples holds the latest report per instance slot.
	samples map[string]model.LoadSample
	exists  map[string]bool // slot ever existed in this fixture
	// demand signal
	hasDemand bool
	demand    model.DemandSignal

	// Clock is overridable in tests; defaults to wall-clock unix seconds and
	// is only used to timestamp the mutation log.
	Clock func() int64

	// Fault injection hooks — tests flip these to force a specific
	// FailureClass. nil means healthy.
	FailSetReplicas error
	FailCurrentRead error
	FailMetricRead  error
	FailDemandRead  error

	// applyLog records every successful SetReplicas for test assertions.
	applyLog []ApplyRecord
}

// ApplyRecord is one observed mutation of the fixture.
type ApplyRecord struct {
	At       int64
	Replicas int
}

// NewLocalFixture builds a fixture with slots instance-001..instance-NNN
// pre-provisioned up to cfg.MaxReplicas and the fleet initially at
// initialReplicas. Slot identities never move.
func NewLocalFixture(cfg model.Config, initialReplicas int) *LocalFixture {
	f := &LocalFixture{
		cfg:      cfg,
		replicas: initialReplicas,
		maxSlots: cfg.MaxReplicas,
		samples:  map[string]model.LoadSample{},
		exists:   map[string]bool{},
		Clock:    func() int64 { return time.Now().Unix() },
	}
	for i := 1; i <= cfg.MaxReplicas; i++ {
		id := fmt.Sprintf("instance-%03d", i)
		f.instances = append(f.instances, id)
		f.exists[id] = true
	}
	return f
}

// CurrentReplicas implements controller.Fleet.
func (f *LocalFixture) CurrentReplicas() (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.FailCurrentRead != nil {
		return 0, f.FailCurrentRead
	}
	return f.replicas, nil
}

// SetReplicas implements controller.Fleet. It only flips the active slot
// range; data for deactivated slots is retained so a scale-up sees them as
// "missing" until they report again (realistic cold behaviour).
func (f *LocalFixture) SetReplicas(n int) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.FailSetReplicas != nil {
		return f.FailSetReplicas
	}
	if n < 0 || n > f.maxSlots {
		return fmt.Errorf("adapter: requested %d replicas outside [0,%d]", n, f.maxSlots)
	}
	f.replicas = n
	f.applyLog = append(f.applyLog, ApplyRecord{At: f.Clock(), Replicas: n})
	return nil
}

// ActiveInstances returns the instance identities currently in the fleet.
func (f *LocalFixture) ActiveInstances() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]string, f.replicas)
	copy(out, f.instances[:f.replicas])
	return out
}

// SubmitSample accepts a per-instance report at clock time now. Unknown
// instance or future timestamp are rejected; stale timestamps are accepted and
// classified later by the controller.
func (f *LocalFixture) SubmitSample(s model.LoadSample, now int64) error {
	if err := model.ValidateSample(s, now); err != nil {
		return err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.exists[s.InstanceID] {
		return fmt.Errorf("unknown instance %q (not a provisioned slot)", s.InstanceID)
	}
	// Only currently-active instances may report: a report from a slot above
	// the current replica count is an identity mismatch, not a metric.
	idx := slotIndex(s.InstanceID)
	if idx < 0 || idx >= f.replicas {
		return fmt.Errorf("instance %q is not in the active fleet of %d", s.InstanceID, f.replicas)
	}
	f.samples[s.InstanceID] = s
	return nil
}

// PostDemand records the out-of-band signal used by the zero-replica policy.
func (f *LocalFixture) PostDemand(present bool, at int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.hasDemand = true
	f.demand = model.DemandSignal{Present: present, ReportedAt: at}
}

// LatestSamples implements controller.MetricSource: one entry per active slot.
func (f *LocalFixture) LatestSamples(now int64) ([]model.Sample, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.FailMetricRead != nil {
		return nil, f.FailMetricRead
	}
	out := make([]model.Sample, 0, f.replicas)
	for i := 0; i < f.replicas; i++ {
		id := f.instances[i]
		s, ok := f.samples[id]
		if !ok {
			out = append(out, model.Sample{InstanceID: id, Missing: true})
			continue
		}
		out = append(out, model.Sample{
			InstanceID: id,
			Load:       s.Load,
			ReportedAt: s.ReportedAt,
			Missing:    false,
		})
	}
	return out, nil
}

// LatestDemand implements controller.MetricSource.
func (f *LocalFixture) LatestDemand(now int64) (model.DemandSignal, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.FailDemandRead != nil {
		return model.DemandSignal{}, false, f.FailDemandRead
	}
	if !f.hasDemand {
		return model.DemandSignal{}, false, nil
	}
	return f.demand, true, nil
}

// ReplayApplies returns a copy of the mutation log (test support).
func (f *LocalFixture) ReplayApplies() []ApplyRecord {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]ApplyRecord, len(f.applyLog))
	copy(out, f.applyLog)
	return out
}

// ClearFaults resets every injection hook.
func (f *LocalFixture) ClearFaults() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.FailSetReplicas = nil
	f.FailCurrentRead = nil
	f.FailMetricRead = nil
	f.FailDemandRead = nil
}

// Snapshot is an explanatory, deterministic view of fixture state.
type Snapshot struct {
	Replicas        int              `json:"replicas"`
	ActiveInstances []string         `json:"active_instances"`
	LatestSamples   map[string]int64 `json:"latest_reported_at"`
	DemandPresent   *bool            `json:"demand_present,omitempty"`
	DemandAt        int64            `json:"demand_reported_at,omitempty"`
}

// Snapshot copies current state for the status endpoint.
func (f *LocalFixture) Snapshot() Snapshot {
	f.mu.Lock()
	defer f.mu.Unlock()
	sn := Snapshot{Replicas: f.replicas, LatestSamples: map[string]int64{}}
	sn.ActiveInstances = append(sn.ActiveInstances, f.instances[:f.replicas]...)
	ids := make([]string, 0, len(f.samples))
	for id := range f.samples {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		sn.LatestSamples[id] = f.samples[id].ReportedAt
	}
	if f.hasDemand {
		p := f.demand.Present
		sn.DemandPresent = &p
		sn.DemandAt = f.demand.ReportedAt
	}
	return sn
}

// slotIndex converts instance-NNN to its 0-based slot index, or -1.
func slotIndex(id string) int {
	var n int
	if _, err := fmt.Sscanf(id, "instance-%d", &n); err != nil {
		return -1
	}
	return n - 1
}
