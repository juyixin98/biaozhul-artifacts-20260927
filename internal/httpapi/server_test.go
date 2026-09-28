package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"infraplanner/internal/journal"
	"infraplanner/internal/model"
	"infraplanner/internal/provider"
	"infraplanner/internal/reconciler"
)

func newTestServer(t *testing.T) (*Server, *provider.Sim) {
	t.Helper()
	store, err := journal.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	sim := provider.NewSim()
	rec := reconciler.New(store, sim, 3)
	return New(rec, sim), sim
}

func do(t *testing.T, h http.Handler, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	out := map[string]any{}
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func stackBody() map[string]any {
	return map[string]any{"resources": []map[string]any{
		{"kind": "vpc", "name": "main",
			"attrs": map[string]string{"cidr": "10/8", "region": "east"}},
		{"kind": "subnet", "name": "web",
			"attrs": map[string]string{"cidr": "10.1/16", "zone": "z1"},
			"refs":  map[string]any{"network_ref": map[string]string{"kind": "vpc", "name": "main"}}},
	}}
}

func TestHTTP_PlanApplyLiveEndToEnd(t *testing.T) {
	s, sim := newTestServer(t)

	status, body := do(t, s.Mux, "POST", "/v1/plans", stackBody())
	if status != http.StatusOK {
		t.Fatalf("plan status=%d body=%v", status, body)
	}
	runID, _ := body["run_id"].(string)
	if runID == "" {
		t.Fatal("missing run_id")
	}

	status, body = do(t, s.Mux, "POST", "/v1/runs/"+runID+"/apply", nil)
	if status != http.StatusOK {
		t.Fatalf("apply status=%d body=%v", status, body)
	}
	if body["state"] != "succeeded" {
		t.Fatalf("state=%v", body["state"])
	}

	status, live := do(t, s.Mux, "GET", "/v1/live", nil)
	if status != http.StatusOK {
		t.Fatalf("live status=%d", status)
	}
	res, _ := live["resources"].([]any)
	if len(res) != 2 {
		t.Fatalf("live resources = %d, want 2", len(res))
	}
	_ = sim

	status, ev := do(t, s.Mux, "GET", "/v1/runs/"+runID+"/evidence", nil)
	if status != http.StatusOK {
		t.Fatalf("evidence status=%d", status)
	}
	if arr, _ := ev["evidence"].([]any); len(arr) == 0 {
		t.Fatal("expected persisted evidence")
	}
}

func TestHTTP_InvalidSpecIs400(t *testing.T) {
	s, _ := newTestServer(t)
	bad := map[string]any{"resources": []map[string]any{
		{"kind": "vpc", "name": "v", "attrs": map[string]string{"cidr": "10/8"}}, // missing region
	}}
	status, body := do(t, s.Mux, "POST", "/v1/plans", bad)
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400", status)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["category"] != model.CatInput {
		t.Fatalf("category=%v, want %s", errObj["category"], model.CatInput)
	}
}

func TestHTTP_BadJSONIs400(t *testing.T) {
	s, _ := newTestServer(t)
	req := httptest.NewRequest("POST", "/v1/plans", bytes.NewReader([]byte("{not json")))
	rec := httptest.NewRecorder()
	s.Mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400", rec.Code)
	}
}

func TestHTTP_UnknownRunIs400(t *testing.T) {
	s, _ := newTestServer(t)
	status, body := do(t, s.Mux, "POST", "/v1/runs/nope/apply", nil)
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400 body=%v", status, body)
	}
}

func TestHTTP_GuardHeldIs409_ReleasedIs200(t *testing.T) {
	s, sim := newTestServer(t)
	sim.Seed(model.Live{
		Key:       model.Key{Kind: model.KindVPC, Name: "critical"},
		ID:        "id-crit",
		Attrs:     map[string]string{"cidr": "10/8", "region": "east"},
		Protected: true,
	})
	// delete without release -> 409 guard_held
	status, body := do(t, s.Mux, "POST", "/v1/plans", map[string]any{"resources": []any{}})
	if status != http.StatusConflict {
		t.Fatalf("status=%d, want 409 body=%v", status, body)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["code"] != "guard_held" {
		t.Fatalf("code=%v, want guard_held", errObj["code"])
	}

	// with explicit release
	rel := map[string]any{"resources": []any{}, "release_guards": []map[string]string{
		{"kind": "vpc", "name": "critical"},
	}}
	status, body = do(t, s.Mux, "POST", "/v1/plans", rel)
	if status != http.StatusOK {
		t.Fatalf("released plan status=%d body=%v", status, body)
	}
	runID, _ := body["run_id"].(string)
	status, body = do(t, s.Mux, "POST", "/v1/runs/"+runID+"/apply", nil)
	if status != http.StatusOK || body["state"] != "succeeded" {
		t.Fatalf("apply after release status=%d body=%v", status, body)
	}
}

func TestHTTP_ExhaustionIs507(t *testing.T) {
	s, sim := newTestServer(t)
	status, body := do(t, s.Mux, "POST", "/v1/plans", stackBody())
	if status != http.StatusOK {
		t.Fatal(body)
	}
	runID, _ := body["run_id"].(string)
	sim.ArmFault(provider.Fault{
		Kind:      provider.FaultCreateExhaust,
		Target:    model.Key{Kind: model.KindVPC, Name: "main"},
		Remaining: 10,
	})
	status, body = do(t, s.Mux, "POST", "/v1/runs/"+runID+"/apply", nil)
	if status != http.StatusInsufficientStorage {
		t.Fatalf("status=%d, want 507", status)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["category"] != model.CatExhaustion {
		t.Fatalf("category=%v, want resource_exhaustion", errObj["category"])
	}
}

func TestHTTP_AdminSeedFaultAndReset(t *testing.T) {
	s, sim := newTestServer(t)
	// seed a resource
	seed := map[string]any{"resources": []map[string]any{
		{"key": map[string]string{"kind": "vpc", "name": "x"},
			"id": "id-x", "attrs": map[string]string{"cidr": "10/8", "region": "east"}},
	}}
	status, _ := do(t, s.Mux, "POST", "/v1/admin/seed", seed)
	if status != http.StatusOK {
		t.Fatalf("seed status=%d", status)
	}
	if obs, _ := sim.Observe(context.Background()); len(obs.Resources) != 1 {
		t.Fatalf("seeded count=%d", len(obs.Resources))
	}
	// arm + list a fault
	status, body := do(t, s.Mux, "POST", "/v1/admin/faults", map[string]any{
		"kind": "delete_transient", "remaining": 2,
		"target": map[string]string{"kind": "vpc", "name": "x"},
	})
	if status != http.StatusOK {
		t.Fatalf("arm status=%d body=%v", status, body)
	}
	status, body = do(t, s.Mux, "GET", "/v1/admin/faults", nil)
	if status != http.StatusOK {
		t.Fatalf("list faults status=%d", status)
	}
	if arr, _ := body["faults"].([]any); len(arr) != 1 {
		t.Fatalf("faults = %v", body)
	}
	// reset clears world and faults
	status, _ = do(t, s.Mux, "POST", "/v1/admin/reset", nil)
	if status != http.StatusOK {
		t.Fatalf("reset status=%d", status)
	}
	if obs, _ := sim.Observe(context.Background()); len(obs.Resources) != 0 {
		t.Fatalf("after reset count=%d", len(obs.Resources))
	}
	if len(sim.Faults()) != 0 {
		t.Fatalf("after reset faults=%v", sim.Faults())
	}
}
