package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"placer/internal/config"
	"placer/internal/logx"
	"placer/internal/model"
	"placer/internal/reconcile"
	"placer/internal/server"
	"placer/internal/store"
)

func newTestServer(t *testing.T) (*httptest.Server, *store.Store) {
	t.Helper()
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	cfg := config.Default()
	loop := reconcile.New(st, cfg, logx.New(nil, 0))
	h := server.New(st, loop, logx.New(nil, 0)).Routes()
	return httptest.NewServer(h), st
}

func doJSON(t *testing.T, srv *httptest.Server, method, path string, body any, runID string) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, _ := http.NewRequest(method, srv.URL+path, rdr)
	req.Header.Set("Content-Type", "application/json")
	if runID != "" {
		req.Header.Set("X-Run-Id", runID)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	out := map[string]any{}
	dec := json.NewDecoder(res.Body)
	_ = dec.Decode(&out)
	return res.StatusCode, out
}

func TestServer_HealthAndVersion(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	code, body := doJSON(t, srv, http.MethodGet, "/healthz", nil, "")
	if code != 200 || body["status"] != "ok" {
		t.Fatalf("healthz wrong: %d %+v", code, body)
	}
	code, body = doJSON(t, srv, http.MethodGet, "/version", nil, "")
	if code != 200 || body["version"] == nil || body["version"] == "" {
		t.Fatalf("version must be present: %d %+v", code, body)
	}
}

// TestServer_PlanFeasibleAndCorrelated verifies the stateless plan
// endpoint returns a feasible result whose run id matches the supplied
// correlation header.
func TestServer_PlanFeasibleAndCorrelated(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	req := model.PlanRequest{
		Nodes: []model.Node{{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 4000, Memory: 1e10, Storage: 1e11}}},
		Instances: []model.Instance{{ID: "p1", State: model.StatePending,
			Request: model.Resources{MilliCPU: 500, Memory: 1, Storage: 1}}},
	}
	code, body := doJSON(t, srv, http.MethodPost, "/v1/plans", req, "run-http-123")
	if code != 200 {
		t.Fatalf("expected 200, got %d body=%+v", code, body)
	}
	if body["run_id"] != "run-http-123" {
		t.Fatalf("run id not correlated: %+v", body["run_id"])
	}
	if body["feasible"] != true || body["solver"] != "exact" {
		t.Fatalf("unexpected body: %+v", body)
	}
}

// TestServer_PlanConflictIs409 not 2xx — rule 4 at the adapter boundary.
func TestServer_PlanConflictIs409(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	req := model.PlanRequest{
		Nodes: []model.Node{{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 100, Memory: 1, Storage: 1}}},
		Instances: []model.Instance{{ID: "p1", State: model.StatePending,
			Request: model.Resources{MilliCPU: 5000, Memory: 1, Storage: 1}}},
	}
	code, body := doJSON(t, srv, http.MethodPost, "/v1/plans", req, "run-409")
	if code != http.StatusConflict {
		t.Fatalf("expected 409, got %d body=%+v", code, body)
	}
	if body["feasible"] != false {
		t.Fatal("409 body must explicitly say feasible=false")
	}
	conflicts, _ := body["conflicts"].([]any)
	if len(conflicts) != 1 {
		t.Fatalf("expected one conflict, got %+v", body["conflicts"])
	}
}

// TestServer_InvalidPlanIs400 distinguishes bad input from conflicts.
func TestServer_InvalidPlanIs400(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	// Unknown JSON field is rejected by DisallowUnknownFields.
	raw := `{"nodes":[],"instances":[],"bogus":1}`
	res, err := http.Post(srv.URL+"/v1/plans", "application/json", strings.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400 for malformed body, got %d", res.StatusCode)
	}
}

// TestServer_InventoryAndReconcile drives the full inventory -> reconcile
// path over HTTP and then reads back the stored run and events.
func TestServer_InventoryAndReconcile(t *testing.T) {
	srv, st := newTestServer(t)
	defer srv.Close()
	ctx := context.Background()

	n := model.Node{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
		Capacity: model.Resources{MilliCPU: 4000, Memory: 1e10, Storage: 1e11}}
	if code, body := doJSON(t, srv, http.MethodPut, "/v1/nodes/a1", n, ""); code != 200 {
		t.Fatalf("put node %d %+v", code, body)
	}
	in := model.Instance{ID: "p1", State: model.StatePending,
		Request: model.Resources{MilliCPU: 500, Memory: 1, Storage: 1}}
	if code, body := doJSON(t, srv, http.MethodPut, "/v1/instances/p1", in, ""); code != 200 {
		t.Fatalf("put instance %d %+v", code, body)
	}

	code, body := doJSON(t, srv, http.MethodPost,
		"/v1/reconcile?run_id=run-e2e-1", nil, "")
	if code != 200 {
		t.Fatalf("reconcile expected 200, got %d %+v", code, body)
	}
	if body["status"] != "feasible" {
		t.Fatalf("reconcile status wrong: %+v", body)
	}
	if _, status, err := st.RunStatus(ctx, "run-e2e-1"); err != nil || status != "feasible" {
		t.Fatalf("run not persisted: status=%s err=%v", status, err)
	}
	evs, err := st.Events(ctx, "run-e2e-1")
	if err != nil || len(evs) != 1 {
		t.Fatalf("expected one durable event, got %+v err=%v", evs, err)
	}
}

// TestServer_BadMethodAndMissingResource check the adapter does not mask
// routing problems with success codes.
func TestServer_BadMethodAndMissingResource(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/v1/plans", nil)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", res.StatusCode)
	}

	code, _ := doJSON(t, srv, http.MethodGet, "/v1/runs/does-not-exist", nil, "")
	if code != http.StatusNotFound {
		t.Fatalf("missing run expected 404, got %d", code)
	}
}
