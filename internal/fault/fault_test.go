// Package fault_test holds the fault-injection suite. It exercises the
// controller against failing adapters and asserts the SPECIFIC failure
// category and the conservative outcome (no silent partial action), rather
// than merely that an interface is callable.
package fault_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/controller"
	"replicactl/internal/model"
	"replicactl/internal/testkit"
)

func cfg() config.Config {
	c := config.Default()
	c.ScaleDownStableWindow.Duration = 5 * time.Minute
	c.MetricFreshness.Duration = 60 * time.Second
	c.MinReplicas = 2
	return c
}

func codes(d *model.Decision) map[string]bool {
	m := map[string]bool{}
	for _, r := range d.Reasons {
		m[r.Code] = true
	}
	return m
}

// Actuator refusing a scale up must leave the fleet unchanged and record the
// ACTUATOR_ERROR failure category; desired/applied reconcile to current.
func TestActuatorFailureScaleUp(t *testing.T) {
	h := testkit.NewHarness(t, 3, cfg())
	h.Port.FailApply = errors.New("fixture: cloud API quota exhausted")
	h.Port.ReportAll(300) // wants to grow to 6

	d, err := h.Engine.Reconcile(context.Background(), "req-fault-apply")
	if err != nil {
		t.Fatalf("actuator failure must be recorded on decision, not returned: %v", err)
	}
	if d.ActuatorError == "" {
		t.Fatalf("expected actuator_error populated")
	}
	if !codes(d)["ACTUATOR_ERROR"] {
		t.Fatalf("missing ACTUATOR_ERROR category, got %v", codes(d))
	}
	if d.Action != "none" || d.DesiredReplicas != 3 || d.AppliedReplicas != 3 {
		t.Fatalf("failed scale up must hold at 3, got action=%s desired=%d applied=%d",
			d.Action, d.DesiredReplicas, d.AppliedReplicas)
	}
	if ids := h.Port.FleetIDs(); len(ids) != 3 {
		t.Fatalf("fleet must be unchanged after actuator refusal, got %v", ids)
	}
	if len(d.ScaledUpIDs) != 0 {
		t.Fatalf("no instances may be added on failed actuation, got %v", d.ScaledUpIDs)
	}
}

// A failure while recording the stable-window recommendation must cause a
// conservative hold with a specific blocking category.
func TestSaveObservationFailureHolds(t *testing.T) {
	h := testkit.NewHarness(t, 4, cfg())
	h.Port.FailSaveObs = errors.New("fixture: disk full")
	h.Port.ReportAll(10) // would like to shrink toward min 2

	d, err := h.Engine.Reconcile(context.Background(), "req-fault-saveobs")
	if err != nil {
		t.Fatalf("observation write failure must degrade to hold, not error: %v", err)
	}
	if !codes(d)[controller.ReasonDownscaleWindowNotMet] {
		t.Fatalf("want DOWNSCALE_STABLE_WINDOW_NOT_MET, got %v", codes(d))
	}
	if d.Action != "none" || d.DesiredReplicas != 4 {
		t.Fatalf("must hold at 4, got action=%s desired=%d", d.Action, d.DesiredReplicas)
	}
}

// A failure reading window history must also hold conservatively.
func TestReadObservationsFailureHolds(t *testing.T) {
	h := testkit.NewHarness(t, 4, cfg())
	h.Port.FailReadObs = errors.New("fixture: cannot read history")
	h.Port.ReportAll(10)

	d, err := h.Engine.Reconcile(context.Background(), "req-fault-readobs")
	if err != nil {
		t.Fatalf("history read failure must degrade to hold, not error: %v", err)
	}
	if !codes(d)[controller.ReasonDownscaleWindowNotMet] {
		t.Fatalf("want DOWNSCALE_STABLE_WINDOW_NOT_MET, got %v", codes(d))
	}
	if d.DesiredReplicas != 4 {
		t.Fatalf("must hold at 4, got %d", d.DesiredReplicas)
	}
}

// Inability to read samples is an infrastructure error: no decision can be
// formed, so the tick returns an error rather than guessing.
func TestSamplesReadFailure(t *testing.T) {
	h := testkit.NewHarness(t, 3, cfg())
	h.Port.FailSamples = errors.New("fixture: telemetry store down")

	if _, err := h.Engine.Reconcile(context.Background(), "req-fault-samples"); err == nil {
		t.Fatalf("sample read failure must be returned as an error")
	}
}

// Inability to persist the audit decision is surfaced (no silent success).
func TestDecisionWriteFailure(t *testing.T) {
	h := testkit.NewHarness(t, 3, cfg())
	h.Port.FailSaveDecision = errors.New("fixture: audit log unavailable")
	h.Port.ReportAll(200)

	if _, err := h.Engine.Reconcile(context.Background(), "req-fault-audit"); err == nil {
		t.Fatalf("decision write failure must be returned")
	}
}

// At zero replicas, an unreadable demand signal is treated as "no fresh
// demand": the system stays at zero and names the reason rather than guessing.
func TestZeroDemandReadFailureHoldsAtZero(t *testing.T) {
	h := testkit.NewHarness(t, 0, cfg())
	h.Port.FailDemand = errors.New("fixture: queue probe failed")

	d, err := h.Engine.Reconcile(context.Background(), "req-fault-demand")
	if err != nil {
		t.Fatalf("demand read failure must degrade to hold-at-zero, not error: %v", err)
	}
	if !codes(d)[controller.ReasonZeroNoDemand] {
		t.Fatalf("want ZERO_NO_FRESH_DEMAND, got %v", codes(d))
	}
	if d.Action != "none" || d.DesiredReplicas != 0 {
		t.Fatalf("must hold at zero, got action=%s desired=%d", d.Action, d.DesiredReplicas)
	}
}
