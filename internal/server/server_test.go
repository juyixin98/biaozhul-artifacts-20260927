package server_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"igmpv2timer/internal/clock"
	"igmpv2timer/internal/config"
	"igmpv2timer/internal/core"
	"igmpv2timer/internal/server"
	"igmpv2timer/internal/store"
)

func newTestServer(t *testing.T) (*httptest.Server, func()) {
	t.Helper()
	cfg := config.Default()
	cfg.HTTPAddr = "127.0.0.1:0"
	cfg.Timing.QueryInterval = 1_000_000
	cfg.Timing.GroupMembershipInterval = 300
	clk := clock.New()
	c, err := core.New(cfg, clk)
	if err != nil {
		t.Fatal(err)
	}
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	srv := server.New(cfg, clk, c, st, nil)
	ts := httptest.NewServer(srv.Handler())
	return ts, func() { ts.Close(); _ = st.Close() }
}

func postJSON(t *testing.T, url string, body any) (int, map[string]any, string) {
	t.Helper()
	raw, _ := json.Marshal(body)
	resp, err := http.Post(url, "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out, resp.Header.Get("X-Request-ID")
}

func TestHealthAndState(t *testing.T) {
	ts, cleanup := newTestServer(t)
	defer cleanup()

	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 || resp.Header.Get("X-Request-ID") == "" {
		t.Fatalf("health status=%d reqid=%q", resp.StatusCode,
			resp.Header.Get("X-Request-ID"))
	}
}

func TestHTTPLifecycleAndStatusCodes(t *testing.T) {
	ts, cleanup := newTestServer(t)
	defer cleanup()

	// accepted report creates membership
	status, body, _ := postJSON(t, ts.URL+"/events", map[string]any{
		"at_ms": 100, "kind": "report", "iface": "eth0",
		"group": "239.1.2.3", "member": "a", "source_addr": "192.0.2.10",
	})
	if status != http.StatusOK {
		t.Fatalf("report status=%d body=%v", status, body)
	}
	diag := body["diag"].(map[string]any)
	if diag["verdict"] != "ACCEPTED" {
		t.Errorf("verdict=%v", diag["verdict"])
	}

	// malformed group -> 422 with the core's stable rejection reason and a
	// request id (rejected events return the diag envelope).
	status, body, _ = postJSON(t, ts.URL+"/events", map[string]any{
		"at_ms": 110, "kind": "report", "iface": "eth0",
		"group": "10.0.0.1", "member": "a", "source_addr": "192.0.2.10",
	})
	if status != http.StatusUnprocessableEntity {
		t.Errorf("bad group status=%d", status)
	}
	diag2, _ := body["diag"].(map[string]any)
	if diag2 == nil || diag2["reason"] != "bad_group_address" ||
		diag2["verdict"] != "REJECTED" {
		t.Errorf("bad-group diag=%v", body["diag"])
	}
	if body["request_id"] == nil || body["request_id"] == "" {
		t.Error("response must carry request id")
	}

	// stale round -> 409 Conflict
	postJSON(t, ts.URL+"/events", map[string]any{
		"at_ms": 150, "kind": "general_query", "iface": "eth0",
	})
	// timeout the group at 100+300=400
	postJSON(t, ts.URL+"/tick", map[string]any{"to_ms": 400})
	postJSON(t, ts.URL+"/events", map[string]any{
		"at_ms": 420, "kind": "general_query", "iface": "eth0",
	})
	status, body, _ = postJSON(t, ts.URL+"/events", map[string]any{
		"at_ms": 430, "kind": "report", "iface": "eth0",
		"group": "239.1.2.3", "member": "a", "source_addr": "192.0.2.10",
		"response_to": "1",
	})
	if status != http.StatusConflict {
		t.Errorf("stale status=%d body=%v", status, body)
	}

	// backward clock -> 422
	status, _, _ = postJSON(t, ts.URL+"/events", map[string]any{
		"at_ms": 1, "kind": "report", "iface": "eth0",
		"group": "239.1.2.3", "member": "a", "source_addr": "192.0.2.10",
	})
	if status != http.StatusUnprocessableEntity {
		t.Errorf("backward clock status=%d", status)
	}

	// unknown field -> 400 bad_json
	raw := strings.NewReader(`{"at_ms":500,"kind":"report","iface":"eth0","bogus":1}`)
	resp, err := http.Post(ts.URL+"/events", "application/json", raw)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("unknown field status=%d", resp.StatusCode)
	}

	// state is empty after timeout
	r, err := http.Get(ts.URL + "/state")
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	var stBody map[string]any
	_ = json.NewDecoder(r.Body).Decode(&stBody)
	snap := stBody["snapshot"].(map[string]any)
	groups, _ := snap["groups"].([]any)
	if len(groups) != 0 {
		t.Errorf("groups after timeout=%v", groups)
	}
}
