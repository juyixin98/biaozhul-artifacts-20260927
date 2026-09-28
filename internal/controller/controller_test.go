package controller_test

import (
	"testing"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/controller"
	"replicactl/internal/model"
	"replicactl/internal/testkit"
)

func testCfg() config.Config {
	c := config.Default()
	c.MetricFreshness.Duration = 60 * time.Second
	c.ScaleDownStableWindow.Duration = 5 * time.Minute
	c.ScaleUpMaxFactor = 2.0
	c.ScaleUpMaxDelta = 4
	c.TargetLoadPerInstance = 100
	c.Tolerance = 0.10
	c.MinReplicas = 0
	c.MaxReplicas = 20
	return c
}

func hasCode(d model.Decision, code string) bool {
	for _, r := range d.Reasons {
		if r.Code == code {
			return true
		}
	}
	return false
}

// A rate cap configured to pin the ceiling at current makes a high reading a
// no-op rather than an up-scale.
func TestScaleUpRatePinnedNoOp(t *testing.T) {
	c := testCfg()
	c.ScaleUpMaxFactor = 1.0
	c.ScaleUpMaxDelta = 0
	h := testkit.NewHarness(t, 3, c)
	h.Port.ReportAll(400) // wants 12, ceiling min(3,3)=3 => no change
	d := h.Tick("pinned")
	if d.Action != "none" || d.DesiredReplicas != 3 {
		t.Fatalf("pinned ceiling must be a no-op at 3, got action=%s desired=%d", d.Action, d.DesiredReplicas)
	}
	if !hasCode(d, controller.ReasonScaleUpRateLimited) || !hasCode(d, controller.ReasonDesiredEqualsCurrent) {
		t.Fatalf("want rate-limited + equals-current reasons, got %+v", d.Reasons)
	}
}

// A clear positive load step scales up immediately, and the hand-computed
// target (total/target) is asserted explicitly.
func TestLoadStepUp(t *testing.T) {
	h := testkit.NewHarness(t, 3, testCfg())
	h.Port.ReportAll(200) // 3*200 = 600 load -> ceil(600/100) = 6
	d := h.Tick("step-up")
	if d.Action != "scale_up" || d.DesiredReplicas != 6 || d.AppliedReplicas != 6 {
		t.Fatalf("got action=%s desired=%d applied=%d, want scale_up to 6", d.Action, d.DesiredReplicas, d.AppliedReplicas)
	}
	if d.TotalLoad != 600 || d.MeasuredLoad != 600 || d.ImputedLoad != 0 {
		t.Fatalf("aggregation wrong: measured=%v imputed=%v total=%v", d.MeasuredLoad, d.ImputedLoad, d.TotalLoad)
	}
	if got := len(d.ScaledUpIDs); got != 3 {
		t.Fatalf("expected 3 new instance ids, got %v", d.ScaledUpIDs)
	}
}

// The per-tick rate cap (factor + absolute delta) bounds a huge step.
func TestScaleUpRateLimited(t *testing.T) {
	c := testCfg()
	c.ScaleUpMaxFactor = 2.0
	c.ScaleUpMaxDelta = 4
	h := testkit.NewHarness(t, 2, c)
	h.Port.ReportAll(500) // total 1000 -> desired 10; ceiling min(ceil(2*2)=4, 2+4=6)=4
	d := h.Tick("rate-cap")
	if d.Action != "scale_up" || d.DesiredReplicas != 4 {
		t.Fatalf("got action=%s desired=%d, want capped scale_up to 4", d.Action, d.DesiredReplicas)
	}
	if !hasCode(d, controller.ReasonScaleUpRateLimited) {
		t.Fatalf("missing SCALE_UP_RATE_LIMITED reason: %+v", d.Reasons)
	}
}

// A downscale is refused until the low recommendation spans the full stable
// window, then applied exactly once.
func TestDownscaleStableWindow(t *testing.T) {
	c := testCfg()
	c.MinReplicas = 2
	h := testkit.NewHarness(t, 4, c)

	h.Port.ReportAll(10) // total 40 -> floor(40/100)=0 -> min 2
	d1 := h.Tick("low-1")
	if d1.Action != "none" || d1.DesiredReplicas != 4 {
		t.Fatalf("first low reading must defer, got action=%s desired=%d", d1.Action, d1.DesiredReplicas)
	}
	if !hasCode(d1, controller.ReasonDownscaleWindowFirstObservation) {
		t.Fatalf("want first-observation defer reason")
	}

	h.Advance(150 * time.Second)
	h.Port.ReportAll(10)
	d2 := h.Tick("low-2")
	if d2.Action != "none" {
		t.Fatalf("shrink before window must hold, got %s", d2.Action)
	}
	if !hasCode(d2, controller.ReasonDownscaleWindowNotMet) {
		t.Fatalf("want window-not-met reason")
	}

	h.Advance(150 * time.Second) // first low observation is now exactly 5m old
	h.Port.ReportAll(10)
	d3 := h.Tick("low-3")
	if d3.Action != "scale_down" || d3.DesiredReplicas != 2 || d3.AppliedReplicas != 2 {
		t.Fatalf("after full window want scale_down to 2, got action=%s desired=%d applied=%d",
			d3.Action, d3.DesiredReplicas, d3.AppliedReplicas)
	}
	if len(d3.ScaledDownIDs) != 2 {
		t.Fatalf("want 2 removed ids, got %v", d3.ScaledDownIDs)
	}
}

// One high reading inside the window prevents shrinking below it.
func TestSingleHighReadingInWindowBlocksShrink(t *testing.T) {
	c := testCfg()
	h := testkit.NewHarness(t, 4, c)
	// Pre-seed window history: a high recommendation (4) spanning the window
	// and a recent low one (2).
	if err := h.Port.SaveObservationForTest(h.Clock.Now().Add(-5*time.Minute), 4); err != nil {
		t.Fatal(err)
	}
	if err := h.Port.SaveObservationForTest(h.Clock.Now().Add(-10*time.Second), 2); err != nil {
		t.Fatal(err)
	}
	h.Port.ReportAll(10)
	d := h.Tick("window-high")
	if d.Action != "none" || d.DesiredReplicas != 4 {
		t.Fatalf("window max 4 must block shrink, got action=%s desired=%d", d.Action, d.DesiredReplicas)
	}
}

// A brief spike grows capacity (rate capped); the immediate dip afterwards
// must NOT cause an instant shrink.
func TestShortSpikeNoInstantDownscale(t *testing.T) {
	c := testCfg()
	c.MinReplicas = 2
	h := testkit.NewHarness(t, 3, c)

	h.Port.ReportAll(300) // total 900 -> desired 9, capped to 6
	d1 := h.Tick("spike")
	if d1.Action != "scale_up" || d1.AppliedReplicas != 6 {
		t.Fatalf("spike should scale up to 6, got %s/%d", d1.Action, d1.AppliedReplicas)
	}

	h.Port.DropReports()
	h.Port.ReportAll(10) // back to low load immediately
	d2 := h.Tick("dip")
	if d2.Action != "none" || d2.DesiredReplicas != 6 {
		t.Fatalf("dip right after spike must hold (stable window), got action=%s desired=%d", d2.Action, d2.DesiredReplicas)
	}
	if !hasCode(d2, controller.ReasonDownscaleWindowFirstObservation) {
		t.Fatalf("want deferred downscale reason after spike")
	}
}

// An unreported instance is conservatively assumed to carry target load and
// forbids shrinking.
func TestMissingInstanceBlocksDownscale(t *testing.T) {
	h := testkit.NewHarness(t, 3, testCfg())
	h.Port.ReportAllExcept(10, "ins-0002") // 2*10 measured + 100 imputed = 120
	d := h.Tick("missing")
	if d.Action != "none" || d.DesiredReplicas != 3 {
		t.Fatalf("missing instance must hold at 3, got action=%s desired=%d", d.Action, d.DesiredReplicas)
	}
	if len(d.MissingInstances) != 1 || d.MissingInstances[0] != "ins-0002" {
		t.Fatalf("missing classification wrong: %v", d.MissingInstances)
	}
	if d.ImputedLoad != 100 || d.TotalLoad != 120 {
		t.Fatalf("imputation wrong: imputed=%v total=%v", d.ImputedLoad, d.TotalLoad)
	}
	if !hasCode(d, controller.ReasonMissingBlocksDownscale) {
		t.Fatalf("want missing-blocks-downscale reason")
	}
}

// Expired (late-reported) metrics cannot trigger a scale up.
func TestStaleMetricsHold(t *testing.T) {
	h := testkit.NewHarness(t, 3, testCfg())
	for _, id := range []string{"ins-0001", "ins-0002", "ins-0003"} {
		h.Port.Report(id, 900, 90*time.Second) // very high but 90s old > 60s freshness
	}
	d := h.Tick("stale")
	if d.Action != "none" || d.DesiredReplicas != 3 {
		t.Fatalf("stale metrics must hold, got action=%s desired=%d", d.Action, d.DesiredReplicas)
	}
	if len(d.StaleInstances) != 3 {
		t.Fatalf("want 3 stale, got %v", d.StaleInstances)
	}
	if !hasCode(d, controller.ReasonAllMetricsStale) {
		t.Fatalf("want all-metrics-stale reason")
	}
}

// Load inside the hysteresis band is a no-op.
func TestToleranceBand(t *testing.T) {
	h := testkit.NewHarness(t, 4, testCfg())
	h.Port.ReportAll(105) // ratio 1.05, within ±10%
	d := h.Tick("band")
	if d.Action != "none" || d.DesiredReplicas != 4 {
		t.Fatalf("within tolerance must hold, got %s/%d", d.Action, d.DesiredReplicas)
	}
	if !hasCode(d, controller.ReasonWithinTolerance) {
		t.Fatalf("want within-tolerance reason")
	}
}

// The independent zero-replica policy: no/expired demand holds at zero; a
// fresh positive signal bootstraps.
func TestScaleFromZero(t *testing.T) {
	h := testkit.NewHarness(t, 0, testCfg())

	d0 := h.Tick("zero-nodemand")
	if d0.Action != "none" || d0.DesiredReplicas != 0 || !hasCode(d0, controller.ReasonZeroNoDemand) {
		t.Fatalf("zero with no demand must hold, got %s/%d", d0.Action, d0.DesiredReplicas)
	}

	h.Port.SetDemandAge(4, 90*time.Second) // expired demand
	d1 := h.Tick("zero-expired")
	if d1.Action != "none" || !hasCode(d1, controller.ReasonZeroNoDemand) {
		t.Fatalf("expired demand must not bootstrap")
	}

	h.Port.SetDemand(7) // fresh positive demand
	d2 := h.Tick("zero-fresh")
	if d2.Action != "scale_up" || d2.DesiredReplicas != 1 || d2.AppliedReplicas != 1 {
		t.Fatalf("fresh demand must bootstrap to 1, got %s/%d", d2.Action, d2.DesiredReplicas)
	}
	if !hasCode(d2, controller.ReasonZeroBootstrapped) {
		t.Fatalf("want bootstrap reason")
	}
}
