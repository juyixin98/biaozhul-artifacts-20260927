package server

import (
	"fmt"
	"net/http"
	"testing"

	"replicactl/core/controller"
)

func TestHTTPLoadStepUpAndRateLimit(t *testing.T) {
	h := newHarness(t, 2)
	h.setTick(1000)
	h.report("instance-001", 30, 1000)
	h.report("instance-002", 30, 1000)

	code, body := h.reconcile("step-1")
	if code != http.StatusOK {
		t.Fatalf("status %d body %v", code, body)
	}
	if getString(body, "action") != string(controller.ActionScaleUp) || getInt(body, "desired_replicas") != 4 {
		t.Fatalf("body %v want scale_up/4", body)
	}
	if getString(body, "request_id") != "step-1" {
		t.Fatalf("request id not correlated: %v", body["request_id"])
	}
	reasons := body["reasons"].([]any)
	found := false
	for _, r := range reasons {
		if r == string(controller.ReasonScaleUpRateLimited) {
			found = true
		}
	}
	if !found {
		t.Fatalf("reasons %v want UP_RATE_LIMITED", reasons)
	}
	obs := body["observation"].(map[string]any)
	if getInt(obs, "raw_desired") != 6 || getInt(obs, "fresh_count") != 2 {
		t.Fatalf("observation %v want raw 6 / fresh 2", obs)
	}

	// The decision is retrievable by its correlated request id.
	code, lookup := h.do("GET", "/v1/requests/step-1", "", nil)
	if code != http.StatusOK || getInt(lookup, "desired_replicas") != 4 {
		t.Fatalf("lookup status %d body %v", code, lookup)
	}
}

func TestHTTPStaleReportsAreNoop(t *testing.T) {
	h := newHarness(t, 2)
	h.setTick(1000)
	// Reports arrive late: ReportedAt 900, age 100 > skew 30.
	h.report("instance-001", 100, 900)
	h.report("instance-002", 100, 900)

	code, body := h.reconcile("stale-1")
	if code != http.StatusOK || getString(body, "action") != string(controller.ActionNoop) {
		t.Fatalf("status %d body %v", code, body)
	}
	reasons := body["reasons"].([]any)
	if len(reasons) != 1 || reasons[0] != string(controller.ReasonNoopNoFreshMetrics) {
		t.Fatalf("reasons %v want exactly [NO_FRESH_METRICS]", reasons)
	}
}

func TestHTTPZeroBootstrapFromDemand(t *testing.T) {
	h := newHarness(t, 0)
	h.setTick(2000)

	// No signal yet: stay at zero with an explicit reason.
	_, body := h.reconcile("z-1")
	if getString(body, "action") != string(controller.ActionNoop) ||
		body["reasons"].([]any)[0] != string(controller.ReasonNoopZeroNoDemand) {
		t.Fatalf("body %v want noop/ZERO_NO_DEMAND", body)
	}

	// Stale signal: still zero.
	h.demand(true, 1900)
	_, body = h.reconcile("z-2")
	if body["reasons"].([]any)[0] != string(controller.ReasonNoopZeroDemandStale) {
		t.Fatalf("body %v want ZERO_DEMAND_STALE", body)
	}

	// Fresh signal: bootstrap to exactly 1.
	h.demand(true, 2000)
	code, body := h.reconcile("z-3")
	if code != http.StatusOK || getString(body, "action") != string(controller.ActionScaleUp) ||
		getInt(body, "desired_replicas") != 1 {
		t.Fatalf("body %v want scale_up/1", body)
	}
}

func TestHTTPShortSpikeBlocksDownscale(t *testing.T) {
	h := newHarness(t, 4)
	// Busy period drives the fleet 4 -> 8 and seeds a high raw point.
	h.setTick(1000)
	for _, id := range []string{"instance-001", "instance-002", "instance-003", "instance-004"} {
		h.report(id, 25, 1000)
	}
	if code, body := h.reconcile("busy-1"); getString(body, "action") != string(controller.ActionScaleUp) ||
		getInt(body, "desired_replicas") != 8 {
		t.Fatalf("code %d body %v want scale_up/8", code, body)
	}

	// Ticks 1010..1060: fleet of 8, all reporting idle load. The spike point
	// at 1000 stays inside the 60s window and must block every downscale.
	for tick := int64(1010); tick <= 1060; tick += 10 {
		h.setTick(tick)
		for i := 1; i <= 8; i++ {
			h.report(instID(i), 0, tick)
		}
		code, body := h.reconcile("idle")
		if code != http.StatusOK {
			t.Fatalf("tick %d status %d body %v", tick, code, body)
		}
		if getString(body, "action") != string(controller.ActionNoop) ||
			body["reasons"].([]any)[0] != string(controller.ReasonNoopWindowPending) {
			t.Fatalf("tick %d body %v want noop/WINDOW_PENDING", tick, body)
		}
	}
	// At 1070 the spike leaves the window ([1010,1070]); raw history of zero
	// permits scale-down to 0 (MinReplicas 0).
	h.setTick(1070)
	for i := 1; i <= 8; i++ {
		h.report(instID(i), 0, 1070)
	}
	code, body := h.reconcile("down-1")
	if code != http.StatusOK || getString(body, "action") != string(controller.ActionScaleDown) ||
		getInt(body, "desired_replicas") != 0 {
		t.Fatalf("body %v want scale_down/0, code %d", body, code)
	}
}

func TestHTTPRequestIdentityRoundTrip(t *testing.T) {
	h := newHarness(t, 2)
	h.setTick(3000)
	h.report("instance-001", 10, 3000)
	h.report("instance-002", 10, 3000)
	code, body := h.reconcile("corr-xyz")
	if code != http.StatusOK || getString(body, "request_id") != "corr-xyz" {
		t.Fatalf("body %v", body)
	}
	code, lookup := h.do("GET", "/v1/requests/corr-xyz", "reader-1", nil)
	if code != http.StatusOK || getString(lookup, "request_id") != "corr-xyz" {
		t.Fatalf("lookup %d %v", code, lookup)
	}
	code, miss := h.do("GET", "/v1/requests/does-not-exist", "", nil)
	if code != http.StatusNotFound || getString(miss, "category") != "REQUEST_NOT_FOUND" {
		t.Fatalf("missing lookup %d %v", code, miss)
	}
}

func TestHTTPInvalidMetricIs400(t *testing.T) {
	h := newHarness(t, 1)
	h.setTick(4000)
	code, body := h.do("POST", "/v1/metrics", "", map[string]any{
		"instance_id": "instance-999", "load": 1, "reported_at": 4000,
	})
	if code != http.StatusBadRequest || getString(body, "category") != string(controller.FailureInvalidInput) {
		t.Fatalf("got %d %v want 400/INVALID_INPUT", code, body)
	}
	code, body = h.doRaw("POST", "/v1/metrics", "", `{"load": `)
	if code != http.StatusBadRequest || getString(body, "category") != string(controller.FailureInvalidInput) {
		t.Fatalf("malformed json got %d %v want 400/INVALID_INPUT", code, body)
	}
}

func TestHTTPFaultInjectionCategories(t *testing.T) {
	// metric_read failure on a populated fleet.
	h := newHarness(t, 2)
	h.setTick(5000)
	h.report("instance-001", 30, 5000)
	h.report("instance-002", 30, 5000)
	code, body := h.do("POST", "/v1/admin/faults", "fault-1",
		map[string]any{"metric_read": "synthetic metric outage"})
	if code != http.StatusOK {
		t.Fatalf("fault set %d %v", code, body)
	}
	code, body = h.reconcile("f-metric")
	if code != http.StatusConflict || getString(body, "category") != string(controller.FailureMetricRead) {
		t.Fatalf("got %d %v want 409/METRIC_READ_FAILED", code, body)
	}
	h.do("POST", "/v1/admin/faults", "clear", map[string]any{"metric_read": ""})
	code, body = h.reconcile("f-recovered")
	if code != http.StatusOK || getString(body, "action") != string(controller.ActionScaleUp) {
		t.Fatalf("recovered tick %d %v", code, body)
	}

	// apply failure on scale-up path.
	h.do("POST", "/v1/admin/faults", "fault-2",
		map[string]any{"set_replicas": "synthetic adapter outage"})
	code, body = h.reconcile("f-apply")
	if code != http.StatusConflict || getString(body, "category") != string(controller.FailureAdapterApply) {
		t.Fatalf("got %d %v want 409/ADAPTER_APPLY_FAILED", code, body)
	}
	h.do("POST", "/v1/admin/faults", "clear2", map[string]any{"set_replicas": ""})

	// history read failure on the downscale path.
	h.setTick(5010)
	for i := 1; i <= 4; i++ {
		h.report(instID(i), 0, 5010)
	}
	h.do("POST", "/v1/admin/faults", "fault-3",
		map[string]any{"history_read": "synthetic history outage"})
	code, body = h.reconcile("f-history")
	if code != http.StatusConflict || getString(body, "category") != string(controller.FailureStore) {
		t.Fatalf("got %d %v want 409/STORE_FAILED", code, body)
	}

	// current_read failure.
	h.do("POST", "/v1/admin/faults", "clear3", map[string]any{"history_read": ""})
	h.do("POST", "/v1/admin/faults", "fault-4",
		map[string]any{"current_read": "synthetic fleet read outage"})
	code, body = h.reconcile("f-fleet")
	if code != http.StatusConflict || getString(body, "category") != string(controller.FailureFleetRead) {
		t.Fatalf("got %d %v want 409/FLEET_READ_FAILED", code, body)
	}
}

func TestHTTPDecisionListContainsReasonAndObservation(t *testing.T) {
	h := newHarness(t, 2)
	h.setTick(6000)
	h.report("instance-001", 10, 6000)
	h.report("instance-002", 10, 6000)
	h.reconcile("list-1")
	code, body := h.do("GET", "/v1/decisions?limit=5", "", nil)
	if code != http.StatusOK || getInt(body, "count") != 1 {
		t.Fatalf("%d %v", code, body)
	}
	ds := body["decisions"].([]any)
	first := ds[0].(map[string]any)
	if getString(first, "request_id") != "list-1" || first["observation"] == nil {
		t.Fatalf("decision row missing correlation/observation: %v", first)
	}
}

func instID(i int) string {
	return fmt.Sprintf("instance-%03d", i)
}
