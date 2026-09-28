package controller_test

import (
	"errors"
	"math"
	"testing"

	"replicactl/core/controller"
	"replicactl/core/model"
)

// ---- Hand-written fakes (the core package ships none of these) --------------

type fakeMetrics struct {
	samples   []model.Sample
	sampleErr error
	demand    model.DemandSignal
	hasDemand bool
	demandErr error
}

func (m *fakeMetrics) LatestSamples(now int64) ([]model.Sample, error) {
	if m.sampleErr != nil {
		return nil, m.sampleErr
	}
	return m.samples, nil
}

func (m *fakeMetrics) LatestDemand(now int64) (model.DemandSignal, bool, error) {
	if m.demandErr != nil {
		return model.DemandSignal{}, false, m.demandErr
	}
	return m.demand, m.hasDemand, nil
}

type fakeFleet struct {
	replicas int
	setErr   error
	applied  []int
}

func (f *fakeFleet) CurrentReplicas() (int, error) { return f.replicas, nil }
func (f *fakeFleet) SetReplicas(n int) error {
	if f.setErr != nil {
		return f.setErr
	}
	f.replicas = n
	f.applied = append(f.applied, n)
	return nil
}

type memStore struct {
	decisions []controller.Decision
	appendErr error
}

func (s *memStore) AppendDecision(d controller.Decision) (controller.Decision, error) {
	if s.appendErr != nil {
		return d, s.appendErr
	}
	d.ID = int64(len(s.decisions) + 1)
	s.decisions = append(s.decisions, d)
	return d, nil
}

type memHistory struct {
	points    map[int64]int
	readErr   error
	appendErr error
}

func newMemHistory() *memHistory { return &memHistory{points: map[int64]int{}} }

func (h *memHistory) AppendPoint(p controller.RawPoint) error {
	if h.appendErr != nil {
		return h.appendErr
	}
	h.points[p.At] = p.RawDesired
	return nil
}

func (h *memHistory) RawPointsSince(since, now int64) ([]controller.RawPoint, error) {
	if h.readErr != nil {
		return nil, h.readErr
	}
	var out []controller.RawPoint
	for at := since; at <= now; at++ {
		if v, ok := h.points[at]; ok {
			out = append(out, controller.RawPoint{At: at, RawDesired: v})
		}
	}
	return out, nil
}

// seed fills one evidence point every 10s over [start,end] with rawDesired v.
func (h *memHistory) seed(t *testing.T, start, end int64, v int) {
	t.Helper()
	for at := start; at <= end; at += 10 {
		h.points[at] = v
	}
}

func fresh(id string, load float64, reportedAt, now int64) model.Sample {
	return model.Sample{InstanceID: id, Load: load, ReportedAt: reportedAt}
}

func stale(id string, load float64, age, now int64) model.Sample {
	return model.Sample{InstanceID: id, Load: load, ReportedAt: now - age}
}

func missing(id string) model.Sample {
	return model.Sample{InstanceID: id, Missing: true}
}

func newCtl(t *testing.T, cfg model.Config, m controller.MetricSource, f *fakeFleet, h controller.RawHistory) (*controller.Controller, *memStore) {
	t.Helper()
	s := &memStore{}
	c, err := controller.New(cfg, m, f, s, h)
	if err != nil {
		t.Fatalf("controller.New: %v", err)
	}
	return c, s
}

// ---- Formula / aggregation (hand-computed expectations) ---------------------

func TestLoadStepUp_RateLimited(t *testing.T) {
	// Fleet of 2, each reports load 30 at t=1000 (age 0).
	// total=60, raw=ceil(60/10)=6. Up ceiling=max(floor(2*2),2+1)=4.
	// Expected: scale to exactly 4, one decision, reason UP_RATE_LIMITED.
	const now int64 = 1000
	m := &fakeMetrics{samples: []model.Sample{
		fresh("instance-001", 30, now, now),
		fresh("instance-002", 30, now, now),
	}}
	f := &fakeFleet{replicas: 2}
	c, _ := newCtl(t, model.DefaultConfig(), m, f, newMemHistory())

	d, err := c.Reconcile(now, "r1")
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if d.Action != controller.ActionScaleUp || d.DesiredReplicas != 4 {
		t.Fatalf("got action=%s desired=%d, want scale_up to 4", d.Action, d.DesiredReplicas)
	}
	if !hasReason(d.Reasons, controller.ReasonScaleUpRateLimited) {
		t.Fatalf("reasons %v missing UP_RATE_LIMITED", d.Reasons)
	}
	if len(f.applied) != 1 || f.applied[0] != 4 {
		t.Fatalf("fleet applied %v, want [4]", f.applied)
	}
	// Observation must expose the hand-computed aggregate.
	o := d.Observation
	if o.RawDesired != 6 || o.TotalLoad != 60 || o.AverageLoad != 30 {
		t.Fatalf("aggregate raw=%d total=%v avg=%v, want 6/60/30", o.RawDesired, o.TotalLoad, o.AverageLoad)
	}
	if o.UtilisationRatio != 3.0 {
		t.Fatalf("utilisation=%v want 3.0", o.UtilisationRatio)
	}
}

func TestScaleUpWithoutHittingRateLimit(t *testing.T) {
	// Fleet 4, load 15 each: total 60, raw 6, ceiling max(8,5)=8 -> 6, no rate reason.
	const now int64 = 1000
	m := &fakeMetrics{samples: []model.Sample{
		fresh("i1", 15, now, now), fresh("i2", 15, now, now),
		fresh("i3", 15, now, now), fresh("i4", 15, now, now),
	}}
	f := &fakeFleet{replicas: 4}
	c, _ := newCtl(t, model.DefaultConfig(), m, f, newMemHistory())
	d, err := c.Reconcile(now, "r2")
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if d.Action != controller.ActionScaleUp || d.DesiredReplicas != 6 {
		t.Fatalf("got %s/%d want scale_up/6", d.Action, d.DesiredReplicas)
	}
	if hasReason(d.Reasons, controller.ReasonScaleUpRateLimited) {
		t.Fatalf("unexpected rate-limit reason: %v", d.Reasons)
	}
}

func TestStaleMetricsMustNotTriggerScaleUp(t *testing.T) {
	// Two instances both reporting huge load 100 but aged 31s > skew 30.
	// Expected: noop NO_FRESH_METRICS, fleet stays 2; stale loads excluded
	// from the aggregate (imputed T=10 each -> raw 2).
	const now int64 = 1000
	m := &fakeMetrics{samples: []model.Sample{
		stale("i1", 100, 31, now), stale("i2", 100, 31, now),
	}}
	f := &fakeFleet{replicas: 2}
	c, _ := newCtl(t, model.DefaultConfig(), m, f, newMemHistory())
	d, err := c.Reconcile(now, "r3")
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopNoFreshMetrics) {
		t.Fatalf("got action=%s reasons=%v, want noop/NO_FRESH_METRICS", d.Action, d.Reasons)
	}
	if f.replicas != 2 || len(f.applied) != 0 {
		t.Fatalf("fleet mutated to %d via %v", f.replicas, f.applied)
	}
	if d.Observation.RawDesired != 2 || d.Observation.FreshCount != 0 || d.Observation.StaleCount != 2 {
		t.Fatalf("observation raw=%d fresh=%d stale=%d, want 2/0/2",
			d.Observation.RawDesired, d.Observation.FreshCount, d.Observation.StaleCount)
	}
	if d.Observation.FreshLoadSum != 0 || d.Observation.ImputedLoadSum != 20 {
		t.Fatalf("fresh sum=%v imputed=%v want 0/20", d.Observation.FreshLoadSum, d.Observation.ImputedLoadSum)
	}
}

func TestFreshSkewBoundary(t *testing.T) {
	// Age exactly 30s is still fresh (<= skew); age 31 is stale.
	const now int64 = 1000
	m := &fakeMetrics{samples: []model.Sample{
		fresh("i1", 30, now-30, now),
		stale("i2", 30, 31, now),
	}}
	f := &fakeFleet{replicas: 2}
	c, _ := newCtl(t, model.DefaultConfig(), m, f, newMemHistory())
	d, _ := c.Reconcile(now, "r3b")
	// fresh fraction 0.5 meets MinFreshFraction 0.5; total=30+10=40, raw 4.
	if d.Observation.FreshCount != 1 || d.Observation.StaleCount != 1 {
		t.Fatalf("fresh=%d stale=%d want 1/1", d.Observation.FreshCount, d.Observation.StaleCount)
	}
	if d.Action != controller.ActionScaleUp || d.DesiredReplicas != 4 {
		t.Fatalf("got %s/%d want scale_up/4 (raw=%d total=%v)", d.Action, d.DesiredReplicas,
			d.Observation.RawDesired, d.Observation.TotalLoad)
	}
}

func TestMissingInstancesImputedAtTarget_BlockScaleDown(t *testing.T) {
	// Fleet 4: three fresh at load 5, one never reported (missing).
	// total = 15 + 10(imputed) = 25; avg 6.25; util 0.625; raw 3 < 4.
	// First ever tick: scale-down window not yet covered -> noop WINDOW_PENDING.
	const now int64 = 1000
	m := &fakeMetrics{samples: []model.Sample{
		fresh("i1", 5, now, now), fresh("i2", 5, now, now),
		fresh("i3", 5, now, now), missing("i4"),
	}}
	f := &fakeFleet{replicas: 4}
	c, _ := newCtl(t, model.DefaultConfig(), m, f, newMemHistory())
	d, _ := c.Reconcile(now, "r4")
	if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopWindowPending) {
		t.Fatalf("got %s/%v want noop/WINDOW_PENDING", d.Action, d.Reasons)
	}
	if d.Observation.MissingCount != 1 || d.Observation.ImputedLoadSum != 10 || d.Observation.RawDesired != 3 {
		t.Fatalf("missing=%d imputed=%v raw=%d want 1/10/3",
			d.Observation.MissingCount, d.Observation.ImputedLoadSum, d.Observation.RawDesired)
	}
	if f.replicas != 4 {
		t.Fatalf("fleet changed to %d despite pending window", f.replicas)
	}
}

func TestLowFreshFractionBlocksBothDirections(t *testing.T) {
	// Fleet 4: one fresh at load 0, three missing. Fraction 0.25 < 0.5.
	// Even though imputed total 30 -> raw 3 suggests downscale, the
	// uncertainty gate must hold the fleet at 4.
	const now int64 = 1000
	m := &fakeMetrics{samples: []model.Sample{
		fresh("i1", 0, now, now), missing("i2"), missing("i3"), missing("i4"),
	}}
	f := &fakeFleet{replicas: 4}
	c, _ := newCtl(t, model.DefaultConfig(), m, f, newMemHistory())
	d, _ := c.Reconcile(now, "r5")
	if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopFreshFractionLow) {
		t.Fatalf("got %s/%v want noop/FRESH_FRACTION_LOW", d.Action, d.Reasons)
	}
	if f.replicas != 4 {
		t.Fatalf("fleet changed to %d", f.replicas)
	}
}

func TestToleranceDeadband(t *testing.T) {
	cfg := model.DefaultConfig()
	cases := []struct {
		name    string
		loads   []float64
		wantAct controller.Action
		wantDes int
		reason  controller.Reason
	}{
		{"exact target", []float64{10, 10}, controller.ActionNoop, 2, controller.ReasonNoopWithinTolerance},
		{"upper edge 1.10", []float64{11, 11}, controller.ActionNoop, 2, controller.ReasonNoopWithinTolerance},
		{"lower edge 0.90", []float64{9, 9}, controller.ActionNoop, 2, controller.ReasonNoopWithinTolerance},
		{"just above band", []float64{12, 12}, controller.ActionScaleUp, 3, ""}, // total24 raw3
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			const now int64 = 2000
			samples := []model.Sample{
				fresh("i1", tc.loads[0], now, now),
				fresh("i2", tc.loads[1], now, now),
			}
			m := &fakeMetrics{samples: samples}
			f := &fakeFleet{replicas: 2}
			c, _ := newCtl(t, cfg, m, f, newMemHistory())
			d, _ := c.Reconcile(now, "r-tol")
			if d.Action != tc.wantAct || d.DesiredReplicas != tc.wantDes {
				t.Fatalf("got %s/%d want %s/%d (util=%v)", d.Action, d.DesiredReplicas,
					tc.wantAct, tc.wantDes, d.Observation.UtilisationRatio)
			}
			if tc.reason != "" && !hasReason(d.Reasons, tc.reason) {
				t.Fatalf("missing reason %s in %v", tc.reason, d.Reasons)
			}
		})
	}
}

// ---- Scale-down stable window ------------------------------------------------

func TestScaleDownWaitsFullWindowThenMovesToWindowMaximum(t *testing.T) {
	// Fleet 4, three instances at load 5, one missing -> raw 3, at every tick.
	// Window is 60s; evidence ticks every 10s. Ticks at 1000..1050 must all
	// stay at 4; the tick at 1060 (window [1000,1060], first point at 1000)
	// scales down to 3.
	cfg := model.DefaultConfig()
	const start int64 = 1000
	mkMetrics := func(now int64) *fakeMetrics {
		return &fakeMetrics{samples: []model.Sample{
			fresh("i1", 5, now, now), fresh("i2", 5, now, now),
			fresh("i3", 5, now, now), missing("i4"),
		}}
	}
	f := &fakeFleet{replicas: 4}
	h := newMemHistory()

	for tick := start; tick <= start+50; tick += 10 {
		c2, _ := controller.New(cfg, mkMetrics(tick), f, &memStore{}, h)
		d, err := c2.Reconcile(tick, "wait")
		if err != nil {
			t.Fatalf("tick %d: %v", tick, err)
		}
		if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopWindowPending) {
			t.Fatalf("tick %d got %s/%v, want noop/WINDOW_PENDING", tick, d.Action, d.Reasons)
		}
	}
	cEnd, _ := controller.New(cfg, mkMetrics(start+60), f, &memStore{}, h)
	d, err := cEnd.Reconcile(start+60, "down")
	if err != nil {
		t.Fatalf("final tick: %v", err)
	}
	if d.Action != controller.ActionScaleDown || d.DesiredReplicas != 3 {
		t.Fatalf("got %s/%d, want scale_down/3", d.Action, d.DesiredReplicas)
	}
	if f.replicas != 3 {
		t.Fatalf("fleet at %d want 3", f.replicas)
	}
}

func TestShortSpikeInsideWindowPreventsScaleDown(t *testing.T) {
	// Window evidence: raw desired 2 everywhere except one raw=5 spike at 1030.
	// Current fleet 4. The conservative window maximum is 5 -> capped to 4,
	// which is >= current, so the single short spike must block the downscale.
	cfg := model.DefaultConfig()
	const now int64 = 1060
	h := newMemHistory()
	h.seed(t, 1000, now, 2)
	h.points[1030] = 5 // short spike
	m := &fakeMetrics{samples: []model.Sample{
		fresh("i1", 5, now, now), fresh("i2", 5, now, now),
		missing("i3"), missing("i4"),
	}} // total 30, raw 3 this tick
	f := &fakeFleet{replicas: 4}
	c, _ := newCtl(t, cfg, m, f, h)
	d, err := c.Reconcile(now, "spike")
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopWindowPending) {
		t.Fatalf("got %s/%v, want noop/WINDOW_PENDING (spike blocks downscale)", d.Action, d.Reasons)
	}
	if f.replicas != 4 {
		t.Fatalf("fleet changed to %d", f.replicas)
	}
}

func TestScaleDownGoesToZeroWhenFullyIdle(t *testing.T) {
	// Two fresh instances reporting zero for the full window: raw 0,
	// MinReplicas 0 -> scale to zero. Later wake-up is tested separately.
	cfg := model.DefaultConfig()
	const start int64 = 3000
	mk := func(now int64) *fakeMetrics {
		return &fakeMetrics{samples: []model.Sample{
			fresh("i1", 0, now, now), fresh("i2", 0, now, now),
		}}
	}
	f := &fakeFleet{replicas: 2}
	h := newMemHistory()
	for tick := start; tick < start+60; tick += 10 {
		ct, _ := controller.New(cfg, mk(tick), f, &memStore{}, h)
		if _, err := ct.Reconcile(tick, "idle"); err != nil {
			t.Fatalf("tick %d: %v", tick, err)
		}
	}
	ct, _ := controller.New(cfg, mk(start+60), f, &memStore{}, h)
	d, err := ct.Reconcile(start+60, "to-zero")
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if d.Action != controller.ActionScaleDown || d.DesiredReplicas != 0 {
		t.Fatalf("got %s/%d want scale_down/0", d.Action, d.DesiredReplicas)
	}
}

// ---- Zero-replica bootstrap (independent policy) -----------------------------

func TestZeroReplicaPolicy(t *testing.T) {
	cfg := model.DefaultConfig()
	const now int64 = 4000

	t.Run("no demand signal", func(t *testing.T) {
		m := &fakeMetrics{hasDemand: false}
		f := &fakeFleet{replicas: 0}
		c, _ := newCtl(t, cfg, m, f, newMemHistory())
		d, err := c.Reconcile(now, "z1")
		if err != nil {
			t.Fatal(err)
		}
		if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopZeroNoDemand) {
			t.Fatalf("got %s/%v want noop/ZERO_NO_DEMAND", d.Action, d.Reasons)
		}
	})

	t.Run("present=false", func(t *testing.T) {
		m := &fakeMetrics{hasDemand: true, demand: model.DemandSignal{Present: false, ReportedAt: now}}
		f := &fakeFleet{replicas: 0}
		c, _ := newCtl(t, cfg, m, f, newMemHistory())
		d, _ := c.Reconcile(now, "z2")
		if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopZeroNoDemand) {
			t.Fatalf("got %s/%v want noop/ZERO_NO_DEMAND", d.Action, d.Reasons)
		}
	})

	t.Run("stale demand cannot wake", func(t *testing.T) {
		m := &fakeMetrics{hasDemand: true, demand: model.DemandSignal{Present: true, ReportedAt: now - 31}}
		f := &fakeFleet{replicas: 0}
		c, _ := newCtl(t, cfg, m, f, newMemHistory())
		d, _ := c.Reconcile(now, "z3")
		if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopZeroDemandStale) {
			t.Fatalf("got %s/%v want noop/ZERO_DEMAND_STALE", d.Action, d.Reasons)
		}
		if f.replicas != 0 {
			t.Fatalf("stale demand woke fleet to %d", f.replicas)
		}
	})

	t.Run("fresh demand bootstraps to 1", func(t *testing.T) {
		m := &fakeMetrics{hasDemand: true, demand: model.DemandSignal{Present: true, ReportedAt: now - 30}}
		f := &fakeFleet{replicas: 0}
		c, _ := newCtl(t, cfg, m, f, newMemHistory())
		d, err := c.Reconcile(now, "z4")
		if err != nil {
			t.Fatal(err)
		}
		if d.Action != controller.ActionScaleUp || d.DesiredReplicas != 1 {
			t.Fatalf("got %s/%d want scale_up/1", d.Action, d.DesiredReplicas)
		}
		if !hasReason(d.Reasons, controller.ReasonFromZeroBootstrap) {
			t.Fatalf("missing FROM_ZERO_BOOTSTRAP: %v", d.Reasons)
		}
	})
}

// ---- Bounds -----------------------------------------------------------------

func TestMaxReplicasCap(t *testing.T) {
	cfg := model.DefaultConfig()
	cfg.MaxReplicas = 4
	const now int64 = 5000
	m := &fakeMetrics{samples: []model.Sample{
		fresh("i1", 30, now, now), fresh("i2", 30, now, now),
		fresh("i3", 30, now, now), fresh("i4", 30, now, now),
	}} // total 120, raw 12, clamped 4 == current
	f := &fakeFleet{replicas: 4}
	c, _ := newCtl(t, cfg, m, f, newMemHistory())
	d, _ := c.Reconcile(now, "cap")
	if d.Action != controller.ActionNoop || !hasReason(d.Reasons, controller.ReasonNoopMaxCapped) {
		t.Fatalf("got %s/%v want noop/MAX_REPLICAS_CAPPED", d.Action, d.Reasons)
	}
	if d.Observation.RawDesired != 12 || d.Observation.ClampedDesired != 4 {
		t.Fatalf("raw=%d clamped=%d want 12/4", d.Observation.RawDesired, d.Observation.ClampedDesired)
	}
}

// ---- Failure classes ---------------------------------------------------------

func TestFailureClasses(t *testing.T) {
	boom := errors.New("synthetic boom")
	cfg := model.DefaultConfig()
	const now int64 = 6000
	healthy := []model.Sample{fresh("i1", 30, now, now), fresh("i2", 30, now, now)}

	t.Run("fleet read fails", func(t *testing.T) {
		f := &fakeFleet{replicas: 2}
		// fleet read hook lives only on some adapters; emulate with wrapper.
		fl := &failingFleet{inner: f, currentErr: boom}
		h := newMemHistory()
		s := &memStore{}
		c, _ := controller.New(cfg, &fakeMetrics{samples: healthy}, fl, s, h)
		d, err := c.Reconcile(now, "e1")
		if !errors.Is(err, boom) {
			t.Fatalf("err=%v want synthetic boom", err)
		}
		if d.FailureClass != controller.FailureFleetRead {
			t.Fatalf("class=%s want FLEET_READ_FAILED", d.FailureClass)
		}
	})

	t.Run("metric read fails", func(t *testing.T) {
		m := &fakeMetrics{sampleErr: boom}
		f := &failingFleet{inner: &fakeFleet{replicas: 2}}
		c, _ := controller.New(cfg, m, f, &memStore{}, newMemHistory())
		d, err := c.Reconcile(now, "e2")
		if !errors.Is(err, boom) || d.FailureClass != controller.FailureMetricRead {
			t.Fatalf("err=%v class=%s", err, d.FailureClass)
		}
	})

	t.Run("apply fails", func(t *testing.T) {
		m := &fakeMetrics{samples: healthy}
		f := &failingFleet{inner: &fakeFleet{replicas: 2}, setErr: boom}
		c, _ := controller.New(cfg, m, f, &memStore{}, newMemHistory())
		d, err := c.Reconcile(now, "e3")
		if !errors.Is(err, boom) || d.FailureClass != controller.FailureAdapterApply {
			t.Fatalf("err=%v class=%s", err, d.FailureClass)
		}
	})

	t.Run("history append fails", func(t *testing.T) {
		m := &fakeMetrics{samples: healthy}
		f := &failingFleet{inner: &fakeFleet{replicas: 2}}
		h := newMemHistory()
		h.appendErr = boom
		c, _ := controller.New(cfg, m, f, &memStore{}, h)
		d, err := c.Reconcile(now, "e4")
		if !errors.Is(err, boom) || d.FailureClass != controller.FailureStore {
			t.Fatalf("err=%v class=%s", err, d.FailureClass)
		}
	})

	t.Run("history read fails during downscale", func(t *testing.T) {
		m := &fakeMetrics{samples: []model.Sample{
			fresh("i1", 0, now, now), fresh("i2", 0, now, now)}}
		f := &failingFleet{inner: &fakeFleet{replicas: 2}}
		h := newMemHistory()
		h.readErr = boom
		c, _ := controller.New(cfg, m, f, &memStore{}, h)
		d, err := c.Reconcile(now, "e5")
		if !errors.Is(err, boom) || d.FailureClass != controller.FailureStore {
			t.Fatalf("err=%v class=%s", err, d.FailureClass)
		}
	})

	t.Run("decision append fails after fixture already mutated", func(t *testing.T) {
		m := &fakeMetrics{samples: healthy}
		f := &failingFleet{inner: &fakeFleet{replicas: 2}}
		s := &memStore{appendErr: boom}
		c, _ := controller.New(cfg, m, f, s, newMemHistory())
		d, err := c.Reconcile(now, "e6")
		if !errors.Is(err, boom) || d.FailureClass != controller.FailureStore {
			t.Fatalf("err=%v class=%s", err, d.FailureClass)
		}
		// Pins the documented ordering: SetReplicas succeeded before the
		// decision-row append failed. The caller must not retry blindly.
		if got := f.inner.(*fakeFleet).replicas; got != 4 {
			t.Fatalf("fleet=%d want 4 (mutation happened; persistence failed)", got)
		}
	})
}

type failingFleet struct {
	inner      controller.Fleet
	currentErr error
	setErr     error
}

func (f *failingFleet) CurrentReplicas() (int, error) {
	if f.currentErr != nil {
		return 0, f.currentErr
	}
	return f.inner.CurrentReplicas()
}

func (f *failingFleet) SetReplicas(n int) error {
	if f.setErr != nil {
		return f.setErr
	}
	return f.inner.SetReplicas(n)
}

func hasReason(rs []controller.Reason, want controller.Reason) bool {
	for _, r := range rs {
		if r == want {
			return true
		}
	}
	return false
}

// ---- Config validation -------------------------------------------------------

func TestRejectsInvalidConfig(t *testing.T) {
	bad := model.DefaultConfig()
	bad.TargetLoadPerInstance = 0
	if _, err := controller.New(bad, &fakeMetrics{}, &fakeFleet{}, &memStore{}, newMemHistory()); err == nil {
		t.Fatal("expected error for zero target")
	}
	bad = model.DefaultConfig()
	bad.StaleSkew = 0
	if _, err := controller.New(bad, &fakeMetrics{}, &fakeFleet{}, &memStore{}, newMemHistory()); err == nil {
		t.Fatal("expected error for zero stale skew")
	}
}

func TestCeilFormulaBoundary(t *testing.T) {
	// total exactly divisible by T must not round up: total 20 / T 10 -> raw 2.
	const now int64 = 7000
	m := &fakeMetrics{samples: []model.Sample{
		fresh("i1", 10, now, now), fresh("i2", 10, now, now)}}
	// util exactly 1.0 -> tolerance noop anyway; check raw via a 3-instance fleet:
	m.samples = append(m.samples, fresh("i3", 0, now, now))
	// total 20 over 3 -> raw 2 < 3 -> downscale path (window pending)
	f := &fakeFleet{replicas: 3}
	c, _ := newCtl(t, model.DefaultConfig(), m, f, newMemHistory())
	d, _ := c.Reconcile(now, "ceil")
	if d.Observation.RawDesired != 2 {
		t.Fatalf("raw=%d want 2 (ceil(20/10)=2)", d.Observation.RawDesired)
	}
	if math.Abs(d.Observation.UtilisationRatio-2.0/3.0) > 1e-9 {
		t.Fatalf("util=%v want %v", d.Observation.UtilisationRatio, 2.0/3.0)
	}
}
