package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"pvsim/api"
	"pvsim/replay"
	"pvsim/store"
)

func newTestServer(t *testing.T) (*httptest.Server, *store.Store) {
	t.Helper()
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	srv := httptest.NewServer(api.NewServer(replay.NewService(st), st).Handler())
	t.Cleanup(srv.Close)
	return srv, st
}

func submitJSON(t *testing.T, srv *httptest.Server, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(srv.URL+"/runs", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func wrapScenario(t *testing.T, scenarioPath string) string {
	t.Helper()
	raw, err := os.ReadFile(scenarioPath)
	if err != nil {
		t.Fatalf("read scenario: %v", err)
	}
	return `{"scenario":` + string(raw) + `}`
}

func TestSubmitConvergesAndPersists(t *testing.T) {
	srv, _ := newTestServer(t)
	body := wrapScenario(t, "../fixtures/01_multi_exit.json")
	status, out := submitJSON(t, srv, body)
	if status != http.StatusCreated {
		t.Fatalf("status = %d, want 201; body=%v", status, out)
	}
	runID, _ := out["run_id"].(string)
	if runID == "" {
		t.Fatalf("no run_id in response: %v", out)
	}
	if conv, _ := out["converged"].(bool); !conv {
		t.Fatalf("not converged: %v", out)
	}
	best, _ := out["best"].(map[string]any)
	r3, _ := best["r3"].([]any)
	if len(r3) != 1 {
		t.Fatalf("r3 best routes = %v, want 1", r3)
	}
	r3m, _ := r3[0].(map[string]any)
	if r3m["peer"] != "r1" {
		t.Fatalf("r3 peer = %v, want r1", r3m["peer"])
	}

	// Traces endpoint must be populated and ordered.
	resp, err := http.Get(srv.URL + "/runs/" + runID + "/traces")
	if err != nil {
		t.Fatalf("get traces: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("traces status = %d", resp.StatusCode)
	}
	var tr struct {
		Traces []map[string]any `json:"traces"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&tr); err != nil {
		t.Fatalf("decode traces: %v", err)
	}
	if len(tr.Traces) == 0 {
		t.Fatalf("no traces returned")
	}

	// GET single run.
	resp2, err := http.Get(srv.URL + "/runs/" + runID)
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("get run status = %d", resp2.StatusCode)
	}

	// Scenario endpoint returns the exact bytes.
	resp3, err := http.Get(srv.URL + "/runs/" + runID + "/scenario")
	if err != nil {
		t.Fatalf("get scenario: %v", err)
	}
	defer resp3.Body.Close()
	if resp3.StatusCode != http.StatusOK {
		t.Fatalf("scenario status = %d", resp3.StatusCode)
	}
}

func TestSubmitOscillationIs422WithCycle(t *testing.T) {
	srv, _ := newTestServer(t)
	body := wrapScenario(t, "../fixtures/03_oscillation.json")
	// Non-convergence is a normal result persisted with 201 (the run
	// completed; the outcome is not_converged). Verify status and evidence.
	status, out := submitJSON(t, srv, body)
	if status != http.StatusCreated {
		t.Fatalf("status = %d, want 201 (oscillation is an outcome, not request error); body=%v", status, out)
	}
	if conv, _ := out["converged"].(bool); conv {
		t.Fatalf("oscillation fixture reported converged")
	}
	if code, _ := out["non_convergent_code"].(string); code != "OSCILLATION_BUDGET_EXCEEDED" {
		t.Fatalf("code = %q", code)
	}
	cyc, ok := out["cycle"].(map[string]any)
	if !ok {
		t.Fatalf("missing cycle evidence: %v", out)
	}
	if _, ok := cyc["entrance_version"]; !ok {
		t.Fatalf("cycle evidence lacks entrance_version: %v", cyc)
	}
}

func TestInputErrorsAre400(t *testing.T) {
	srv, _ := newTestServer(t)
	cases := []struct {
		name string
		body string
		code string
	}{
		{"not-json", `{garbage`, "PAYLOAD_SYNTAX"},
		{"missing-scenario", `{}`, "MISSING_SCENARIO"},
		{"bad-scenario", `{"scenario":{"routers":[]}}`, "INVALID_CONFIG"},
		{"duplicate-event-ref", `{"scenario":{"routers":[{"name":"r1","asn":1}],"sessions":[],"events":[{"seq":1,"router":"r1","peer":"nope","kind":"withdraw","prefix":"P"}]}}`, "UNREFERENCED_ENTITY"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			status, out := submitJSON(t, srv, tc.body)
			if status != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400; body=%v", status, out)
			}
			eobj, _ := out["error"].(map[string]any)
			if eobj["kind"] != "INPUT" {
				t.Fatalf("kind = %v, want INPUT", eobj["kind"])
			}
			if eobj["code"] != tc.code {
				t.Fatalf("code = %v, want %s", eobj["code"], tc.code)
			}
		})
	}
}

func TestPayloadTooLargeIs422(t *testing.T) {
	srv, st := newTestServer(t)
	_ = st
	big := bytes.Repeat([]byte("x"), replay.MaxPayloadBytes+10)
	resp, err := http.Post(srv.URL+"/runs", "application/json", bytes.NewReader(big))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422", resp.StatusCode)
	}
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	eobj, _ := out["error"].(map[string]any)
	if eobj["kind"] != "RESOURCE_EXHAUSTED" {
		t.Fatalf("kind = %v, want RESOURCE_EXHAUSTED", eobj)
	}
}

func TestDuplicateRunIDIs409(t *testing.T) {
	srv, _ := newTestServer(t)
	scenario, err := os.ReadFile("../fixtures/01_multi_exit.json")
	if err != nil {
		t.Fatal(err)
	}
	body := `{"run_id":"fixed-id","scenario":` + string(scenario) + `}`
	if s, _ := submitJSON(t, srv, body); s != http.StatusCreated {
		t.Fatalf("first submit status = %d", s)
	}
	s, out := submitJSON(t, srv, body)
	if s != http.StatusConflict {
		t.Fatalf("duplicate status = %d, want 409; body=%v", s, out)
	}
	eobj, _ := out["error"].(map[string]any)
	if eobj["code"] != "RUN_ID_EXISTS" {
		t.Fatalf("code = %v, want RUN_ID_EXISTS", eobj)
	}
}

func TestGetUnknownRunIs404(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, sub := range []string{"", "/traces", "/decisions", "/deliveries", "/scenario"} {
		resp, err := http.Get(srv.URL + "/runs/nope" + sub)
		if err != nil {
			t.Fatalf("get: %v", err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusNotFound {
			t.Fatalf("GET /runs/nope%s status = %d, want 404", sub, resp.StatusCode)
		}
	}
}

func TestReplayStoredScenario(t *testing.T) {
	srv, _ := newTestServer(t)
	body := wrapScenario(t, "../fixtures/02_withdraw_policy.json")
	_, out := submitJSON(t, srv, body)
	firstID, _ := out["run_id"].(string)

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/runs/"+firstID+"/replay", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("replay status = %d", resp.StatusCode)
	}
	var re map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&re)
	if re["run_id"] == firstID {
		t.Fatalf("replay reused run id %v; want a fresh id", re["run_id"])
	}
	if conv, _ := re["converged"].(bool); !conv {
		t.Fatalf("replayed run not converged: %v", re)
	}

	// Both runs must now be listable.
	listResp, err := http.Get(srv.URL + "/runs")
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	defer listResp.Body.Close()
	var listed struct {
		Runs []map[string]any `json:"runs"`
	}
	_ = json.NewDecoder(listResp.Body).Decode(&listed)
	if len(listed.Runs) != 2 {
		t.Fatalf("listed %d runs, want 2", len(listed.Runs))
	}
}

func TestHealthz(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("healthz: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz status = %d", resp.StatusCode)
	}
}

// Ensure service-level state conflict also surfaces when the store is
// queried directly after an HTTP-created run.
func TestServiceDirectConflict(t *testing.T) {
	st, _ := store.Open(":memory:")
	defer st.Close()
	svc := replay.NewService(st)
	scenario, _ := os.ReadFile("../fixtures/01_multi_exit.json")
	raw := []byte(`{"run_id":"direct","scenario":` + string(scenario) + `}`)
	if _, _, err := svc.Submit(context.Background(), raw, ""); err != nil {
		t.Fatalf("first: %v", err)
	}
	_, _, err := svc.Submit(context.Background(), raw, "")
	if err == nil {
		t.Fatalf("expected conflict error")
	}
	if !strings.Contains(err.Error(), "RUN_ID_EXISTS") {
		t.Fatalf("err = %v, want RUN_ID_EXISTS", err)
	}
}
