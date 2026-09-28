package integration_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"fwrule/internal/httpapi"
	"fwrule/internal/store"
)

// TestEndToEndSpinUp exercises the real stack: SQLite persistence, policy
// versioning, analysis endpoint, replay + per-request log correlation.
func TestEndToEndSpinUp(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	db := filepath.Join(dir, "test.db")

	st, err := store.Open(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	srv := httptest.NewServer(httpapi.New(st).Handler())
	defer srv.Close()

	spec, err := os.ReadFile("../testdata/mini-policy.json")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(map[string]json.RawMessage{
		"name": json.RawMessage(`"mini"`),
		"spec": spec,
	})

	// 1. Upload policy -> version 1 with a correlated request id.
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/policies", bytes.NewReader(body))
	req.Header.Set("X-Request-ID", "upload-1")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("upload status=%d", resp.StatusCode)
	}
	var uploaded struct {
		Version int64 `json:"version"`
		Report  struct {
			Diagnostics []map[string]any `json:"diagnostics"`
		} `json:"report"`
	}
	decode(t, resp, &uploaded)
	resp.Body.Close()
	if uploaded.Version != 1 {
		t.Fatalf("version=%d", uploaded.Version)
	}
	if len(uploaded.Report.Diagnostics) == 0 {
		t.Fatal("expected diagnostics for mini policy")
	}

	// 2. Replay a packet with a distinct request id.
	repBody, _ := json.Marshal(map[string]any{
		"request_id": "pkt-42", "protocol": "tcp",
		"src_ip": "10.0.0.0", "dst_ip": "192.168.0.0",
		"src_port": 0, "dst_port": 0,
	})
	resp2, err := http.Post(srv.URL+"/v1/replay", "application/json", bytes.NewReader(repBody))
	if err != nil {
		t.Fatal(err)
	}
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("replay status=%d", resp2.StatusCode)
	}
	resp2.Body.Close()

	// 3. Fetch the log by the SAME request id and verify the trace and
	// version are persisted and explainable.
	resp3, err := http.Get(srv.URL + "/v1/logs/pkt-42")
	if err != nil {
		t.Fatal(err)
	}
	var logged struct {
		Version  int64 `json:"version"`
		Decision struct {
			DecidedBy string `json:"decided_by"`
			Action    string `json:"action"`
			Trace     []struct {
				RuleID  string `json:"rule_id"`
				Matched bool   `json:"matched"`
			} `json:"trace"`
		} `json:"decision"`
	}
	decode(t, resp3, &logged)
	resp3.Body.Close()
	if logged.Version != 1 {
		t.Fatalf("logged version=%d", logged.Version)
	}
	if logged.Decision.DecidedBy != "a-broad-allow" || logged.Decision.Action != "allow" {
		t.Fatalf("logged decision=%+v", logged.Decision)
	}
	if len(logged.Decision.Trace) != 10 {
		t.Fatalf("persisted trace length=%d", len(logged.Decision.Trace))
	}

	// 4. A duplicate request id must be rejected (no silent overwrite).
	resp4, err := http.Post(srv.URL+"/v1/replay", "application/json", bytes.NewReader(repBody))
	if err != nil {
		t.Fatal(err)
	}
	resp4.Body.Close()
	if resp4.StatusCode != http.StatusConflict {
		t.Fatalf("duplicate replay status=%d, want 409", resp4.StatusCode)
	}

	// 5. Analysis endpoint reuses and persists against the named version.
	resp5, err := http.Get(srv.URL + "/v1/analyze?version=1")
	if err != nil {
		t.Fatal(err)
	}
	if resp5.StatusCode != http.StatusOK {
		t.Fatalf("analyze status=%d", resp5.StatusCode)
	}
	resp5.Body.Close()
}

// TestErrorOverHTTP checks malformed replay surfaces a stable JSON error code.
func TestErrorOverHTTP(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(ctx, ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	srv := httptest.NewServer(httpapi.New(st).Handler())
	defer srv.Close()

	spec, err := os.ReadFile("../testdata/default-deny-v4.json")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(map[string]json.RawMessage{"spec": spec})
	resp, err := http.Post(srv.URL+"/v1/policies", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	bad, _ := json.Marshal(map[string]any{
		"protocol": "nope", "src_ip": "10.0.0.1", "dst_ip": "192.168.0.1",
	})
	resp2, err := http.Post(srv.URL+"/v1/replay", "application/json", bytes.NewReader(bad))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d", resp2.StatusCode)
	}
	var env struct {
		Decision struct {
			ErrorCode string `json:"error_code"`
			Status    string `json:"status"`
		} `json:"decision"`
	}
	decode(t, resp2, &env)
	if env.Decision.ErrorCode != "UNKNOWN_PROTOCOL" || env.Decision.Status != "error" {
		t.Fatalf("decision error code=%s status=%s", env.Decision.ErrorCode, env.Decision.Status)
	}
}

func decode(t *testing.T, resp *http.Response, v any) {
	t.Helper()
	if err := json.NewDecoder(resp.Body).Decode(v); err != nil {
		t.Fatalf("decode: %v", err)
	}
}
