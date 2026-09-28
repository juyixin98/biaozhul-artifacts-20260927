// Package testkit provides the deterministic harness shared by unit, fault
// and acceptance tests: an in-memory implementation of controller.Port, a
// controllable clock, and a tick runner that grades every decision against
// the independent oracle package.
package testkit

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"testing"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/controller"
	"replicactl/internal/model"
	"replicactl/internal/oracle"
)

// Clock is a manually advanced clock.
type Clock struct{ t time.Time }

func NewClock(at time.Time) *Clock { return &Clock{t: at.UTC()} }
func (c *Clock) Now() time.Time    { return c.t }
func (c *Clock) Advance(d time.Duration) {
	c.t = c.t.Add(d)
}

// Port is the in-memory controller.Port with fault injection knobs.
type Port struct {
	mu sync.Mutex

	Cfg   config.Config
	rev   int64
	seq   int
	fleet []string

	samples   []model.Sample
	hasDemand bool
	demand    model.Demand
	obs       []controller.Observation
	decisions []model.Decision

	FailApply        error
	FailSaveObs      error
	FailReadObs      error
	FailSamples      error
	FailDemand       error
	FailSaveDecision error
}

// NewPort returns a port seeded with n active instances and given config.
func NewPort(n int, cfg config.Config) *Port {
	p := &Port{Cfg: cfg, rev: 1}
	p.SetFleet(n)
	return p
}

func (p *Port) SetFleet(n int) {
	p.fleet = nil
	for i := 1; i <= n; i++ {
		p.fleet = append(p.fleet, fmt.Sprintf("ins-%04d", i))
	}
	p.seq = n
}

// FleetIDs exposes the active fleet.
func (p *Port) FleetIDs() []string { return append([]string(nil), p.fleet...) }

// Report inserts a sample observed age ago (0 = fresh).
func (p *Port) Report(id string, value float64, age time.Duration) {
	now := time.Time(p.nowForTest())
	p.samples = append(p.samples, model.Sample{
		InstanceID: id, Metric: p.Cfg.Metric, Value: value,
		ObservedAt: now.Add(-age), ReceivedAt: now,
	})
}

// ReportAll reports value for every currently active instance (fresh).
func (p *Port) ReportAll(value float64) {
	for _, id := range p.fleet {
		p.Report(id, value, 0)
	}
}

// ReportAllExcept reports fresh values for all active ids except skip.
func (p *Port) ReportAllExcept(value float64, skip ...string) {
	skipped := map[string]bool{}
	for _, s := range skip {
		skipped[s] = true
	}
	for _, id := range p.fleet {
		if !skipped[id] {
			p.Report(id, value, 0)
		}
	}
}

// DropReports removes all samples (next tick sees everyone missing).
func (p *Port) DropReports() { p.samples = nil }

// SetDemand records a fresh external demand signal.
func (p *Port) SetDemand(pending int64) {
	p.hasDemand = true
	p.demand = model.Demand{Pending: pending, ObservedAt: p.nowForTest(), ReceivedAt: p.nowForTest()}
}

// SetDemandAge records a demand signal of the given age.
func (p *Port) SetDemandAge(pending int64, age time.Duration) {
	now := p.nowForTest()
	p.hasDemand = true
	p.demand = model.Demand{Pending: pending, ObservedAt: now.Add(-age), ReceivedAt: now}
}

// ClearDemand removes any demand signal.
func (p *Port) ClearDemand() { p.hasDemand = false; p.demand = model.Demand{} }

// nowForTest returns the "current" time. Tests set it via SetNow; the harness
// clock is passed separately to the engine.
var externalNow time.Time

func (p *Port) nowForTest() time.Time {
	if !externalNow.IsZero() {
		return externalNow
	}
	return time.Unix(0, 0).UTC()
}

// SetNow aligns fixture timestamps with the harness clock.
func SetNow(t time.Time) { externalNow = t.UTC() }

// --- controller.Port implementation ---------------------------------------

func (p *Port) LoadConfig(context.Context) (config.Config, int64, error) {
	return p.Cfg, p.rev, nil
}
func (p *Port) Fleet(context.Context) (model.Fleet, error) {
	return model.Fleet{Instances: append([]string(nil), p.fleet...), UpdatedAt: externalNow}, nil
}
func (p *Port) LatestSamples(context.Context, time.Time) ([]model.Sample, error) {
	if p.FailSamples != nil {
		return nil, p.FailSamples
	}
	return append([]model.Sample(nil), p.samples...), nil
}
func (p *Port) LatestDemand(context.Context, time.Time) (model.Demand, bool, error) {
	if p.FailDemand != nil {
		return model.Demand{}, false, p.FailDemand
	}
	return p.demand, p.hasDemand, nil
}
func (p *Port) PruneSamples(_ context.Context, before time.Time) error {
	kept := p.samples[:0]
	for _, s := range p.samples {
		if !s.ObservedAt.Before(before) {
			kept = append(kept, s)
		}
	}
	p.samples = kept
	return nil
}
func (p *Port) PruneObservations(_ context.Context, before time.Time) error {
	kept := p.obs[:0]
	for _, o := range p.obs {
		if !o.At.Before(before) {
			kept = append(kept, o)
		}
	}
	p.obs = kept
	return nil
}
func (p *Port) SaveObservation(_ context.Context, at time.Time, replicas int32) error {
	if p.FailSaveObs != nil {
		return p.FailSaveObs
	}
	p.obs = append(p.obs, controller.Observation{At: at, Replicas: replicas})
	return nil
}
func (p *Port) ObservationsSince(_ context.Context, since time.Time) ([]controller.Observation, error) {
	if p.FailReadObs != nil {
		return nil, p.FailReadObs
	}
	var out []controller.Observation
	for _, o := range p.obs {
		if !o.At.Before(since) {
			out = append(out, o)
		}
	}
	return out, nil
}
func (p *Port) ApplyScale(_ context.Context, want int32, at time.Time) ([]string, []string, []string, error) {
	if p.FailApply != nil {
		return nil, nil, nil, p.FailApply
	}
	cur := len(p.fleet)
	var added, removed []string
	switch {
	case int(want) > cur:
		for cur < int(want) {
			p.seq++
			id := fmt.Sprintf("ins-%04d", p.seq)
			p.fleet = append(p.fleet, id)
			added = append(added, id)
			cur++
		}
	case int(want) < cur:
		drop := p.fleet[want:]
		removed = append(removed, drop...)
		p.fleet = append([]string(nil), p.fleet[:want]...)
	}
	return append([]string(nil), p.fleet...), added, removed, nil
}
func (p *Port) SaveDecision(_ context.Context, d model.Decision) error {
	if p.FailSaveDecision != nil {
		return p.FailSaveDecision
	}
	p.decisions = append(p.decisions, d)
	return nil
}

// Decisions returns persisted decision records.
func (p *Port) Decisions() []model.Decision { return p.decisions }

// ObsCount returns the number of stored window observations.
func (p *Port) ObsCount() int { return len(p.obs) }

// SaveObservationForTest pre-seeds window history (used by window tests).
func (p *Port) SaveObservationForTest(at time.Time, replicas int32) error {
	p.obs = append(p.obs, controller.Observation{At: at, Replicas: replicas})
	sort.Slice(p.obs, func(i, j int) bool { return p.obs[i].At.Before(p.obs[j].At) })
	return nil
}

// Harness bundles a port, clock and engine.
type Harness struct {
	T      *testing.T
	Clock  *Clock
	Port   *Port
	Engine *controller.Engine
	tickN  int
}

// NewHarness builds a harness starting at a fixed epoch.
func NewHarness(t *testing.T, n int, cfg config.Config) *Harness {
	start := time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)
	SetNow(start)
	clk := NewClock(start)
	prt := NewPort(n, cfg)
	return &Harness{T: t, Clock: clk, Port: prt, Engine: controller.NewEngine(prt, clk)}
}

// Advance moves both the clock and the fixture timestamp baseline.
func (h *Harness) Advance(d time.Duration) {
	h.Clock.Advance(d)
	SetNow(h.Clock.Now())
}

// Policy converts the active config into the independent oracle policy.
func (h *Harness) Policy() oracle.Policy {
	c := h.Port.Cfg
	return oracle.Policy{
		TargetPerInstance: c.TargetLoadPerInstance,
		MinReplicas:       c.MinReplicas,
		MaxReplicas:       c.MaxReplicas,
		Freshness:         c.MetricFreshness.Duration,
		DemandFreshness:   c.DemandFreshness.Duration,
		StableWindow:      c.ScaleDownStableWindow.Duration,
		UpFactor:          c.ScaleUpMaxFactor,
		UpMaxDelta:        c.ScaleUpMaxDelta,
		Tolerance:         c.Tolerance,
		Bootstrap:         c.BootstrapReplicas,
		FromZeroEnabled:   c.ScaleFromZeroEnabled,
	}
}

// Tick runs one reconciliation and grades it against the independent oracle.
// It returns the produced decision.
func (h *Harness) Tick(label string) model.Decision {
	h.tickN++
	now := h.Clock.Now()

	// Snapshot the exact inputs the engine will see (before it runs/prunes).
	beforeSamples := append([]model.Sample(nil), h.Port.samples...)
	beforeActive := append([]string(nil), h.Port.fleet...)
	prior := append([]controller.Observation(nil), h.Port.obs...)

	rid := fmt.Sprintf("req-%s-t%d", label, h.tickN)
	d, err := h.Engine.Reconcile(context.Background(), rid)
	if err != nil {
		h.T.Fatalf("%s: reconcile returned error: %v", label, err)
	}

	exp := h.expected(now, beforeActive, beforeSamples, prior)
	h.assertMatches(label, exp, *d)
	return *d
}

func (h *Harness) expected(now time.Time, active []string, samples []model.Sample, prior []controller.Observation) oracle.Expectation {
	latest := map[string]model.Sample{}
	for _, s := range samples {
		if cur, ok := latest[s.InstanceID]; !ok || s.ObservedAt.After(cur.ObservedAt) {
			latest[s.InstanceID] = s
		}
	}
	var reports []oracle.InstanceReport
	for _, id := range active {
		s, ok := latest[id]
		r := oracle.InstanceReport{ID: id}
		if ok {
			obs := s.ObservedAt
			r.ObservedAt = &obs
			r.Value = s.Value
		}
		reports = append(reports, r)
	}
	var op []oracle.PriorObservation
	for _, o := range prior {
		op = append(op, oracle.PriorObservation{At: o.At, Replicas: o.Replicas})
	}
	dm := oracle.DemandState{Has: h.Port.hasDemand, Pending: h.Port.demand.Pending, ObservedAt: h.Port.demand.ObservedAt}
	return oracle.Expected(oracle.Input{
		Tick: now, Current: int32(len(active)),
		Active: active, Reports: reports, Demand: dm, Prior: op, Policy: h.Policy(),
	})
}

func (h *Harness) assertMatches(label string, exp oracle.Expectation, d model.Decision) {
	t := h.T
	if d.Action != exp.Action {
		t.Errorf("%s: action = %q, want %q", label, d.Action, exp.Action)
	}
	if d.DesiredReplicas != exp.DesiredReplicas {
		t.Errorf("%s: desired = %d, want %d", label, d.DesiredReplicas, exp.DesiredReplicas)
	}
	if d.DesiredRaw != exp.RawDesired {
		t.Errorf("%s: desired_raw = %d, want %d", label, d.DesiredRaw, exp.RawDesired)
	}
	assertStrings(t, label+":fresh", d.FreshInstances, exp.Fresh)
	assertStrings(t, label+":stale", d.StaleInstances, exp.Stale)
	assertStrings(t, label+":missing", d.MissingInstances, exp.Missing)
	if !floatEq(d.TotalLoad, exp.TotalLoad) {
		t.Errorf("%s: total_load = %v, want %v", label, d.TotalLoad, exp.TotalLoad)
	}
	gotCodes := reasonCodes(d)
	wantCodes := append([]string(nil), exp.ReasonCodes...)
	sort.Strings(gotCodes)
	sort.Strings(wantCodes)
	assertStrings(t, label+":reasons", gotCodes, wantCodes)
	assertStrings(t, label+":uncertainties", d.Uncertainties, exp.Uncertainties)
	if d.AppliedReplicas != d.DesiredReplicas && d.ActuatorError == "" {
		t.Errorf("%s: applied %d != desired %d with no actuator error", label, d.AppliedReplicas, d.DesiredReplicas)
	}
}

func reasonCodes(d model.Decision) []string {
	var out []string
	for _, r := range d.Reasons {
		out = append(out, r.Code)
	}
	return out
}

func assertStrings(t *testing.T, what string, got, want []string) {
	g := append([]string(nil), got...)
	w := append([]string(nil), want...)
	sort.Strings(g)
	sort.Strings(w)
	if len(g) == 0 {
		g = []string{}
	}
	if len(w) == 0 {
		w = []string{}
	}
	if fmt.Sprint(g) != fmt.Sprint(w) {
		t.Errorf("%s = %v, want %v", what, g, w)
	}
}

func floatEq(a, b float64) bool {
	d := a - b
	if d < 0 {
		d = -d
	}
	return d < 1e-9
}
