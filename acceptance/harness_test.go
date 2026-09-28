// Package acceptance_test contains INDEPENDENT black-box tests. They interact
// with the service only over HTTP (httptest in-process here, a real binary in
// restart_test.go) and never import the controller package: expectations are
// re-derived by a reference oracle written solely in this package from the
// declared inputs, so the core cannot generate its own reference answer.
package acceptance_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"rollctl/internal/adapter"
	"rollctl/internal/api"
	"rollctl/internal/controller"
	"rollctl/internal/store"
)

// client is a minimal JSON HTTP client carrying request ids.
type client struct {
	t    *testing.T
	base string
	hc   *http.Client
	rid  string
}

func newClient(t *testing.T, h http.Handler, rid string) *client {
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return &client{t: t, base: srv.URL, hc: srv.Client(), rid: rid}
}

func (c *client) do(method, path string, body any) (map[string]any, http.Header, int) {
	c.t.Helper()
	var rdr io.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		rdr = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, c.base+path, rdr)
	if err != nil {
		c.t.Fatalf("request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if c.rid != "" {
		req.Header.Set("X-Request-Id", c.rid)
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		c.t.Fatalf("http %s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return out, resp.Header, resp.StatusCode
}

func (c *client) mustStatus(code int, got int, body map[string]any) {
	c.t.Helper()
	if got != code {
		c.t.Fatalf("status = %d, want %d; body=%v", got, code, body)
	}
}

// startInProc builds the real composition (store + simulator + api) on a temp
// SQLite file. The test drives time with POST /admin/tick.
func startInProc(t *testing.T, dbPath string, capacity int) *client {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, dbPath)
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	sim, err := adapter.NewSimulator(ctx, st, capacity)
	if err != nil {
		t.Fatalf("sim: %v", err)
	}
	ctl, err := controller.New(ctx, st, sim, controller.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err != nil {
		t.Fatalf("ctl: %v", err)
	}
	h := api.NewServer(ctl, sim, slog.New(slog.NewTextHandler(io.Discard, nil))).Handler()
	return newClient(t, h, "")
}

// setBehavior installs a fixture through the admin HTTP API.
func setBehavior(t *testing.T, c *client, workload, revision string, b map[string]any) {
	t.Helper()
	body, _, code := c.do("PUT",
		"/admin/simulator/workloads/"+workload+"/revisions/"+revision+"/behavior", b)
	if code != 200 {
		t.Fatalf("set behavior %s/%s: %d %v", workload, revision, code, body)
	}
}

type policy struct {
	MaxSurge            int   `json:"maxSurge"`
	MaxUnavailable      int   `json:"maxUnavailable"`
	ReadyThresholdTicks int   `json:"readyThresholdTicks"`
	DeadlineTicks       int64 `json:"deadlineTicks"`
	MaxStartFailures    int   `json:"maxStartFailures"`
}

func createWorkload(t *testing.T, c *client, name string, replicas int, rev string, p policy) {
	t.Helper()
	body, _, code := c.do("POST", "/api/v1/workloads", map[string]any{
		"name": name, "replicas": replicas, "revision": rev, "policy": p,
	})
	if code != 201 {
		t.Fatalf("create workload: %d %v", code, body)
	}
}

func startRelease(t *testing.T, c *client, name, rev string, p *policy) map[string]any {
	t.Helper()
	in := map[string]any{"revision": rev}
	if p != nil {
		in["policy"] = p
	}
	body, hdr, code := c.do("POST", "/api/v1/workloads/"+name+"/releases", in)
	if code != 201 {
		t.Fatalf("create release: %d %v", code, body)
	}
	if hdr.Get("X-Request-Id") == "" {
		t.Fatal("response missing X-Request-Id correlation header")
	}
	return body
}

func tick(c *client) map[string]any {
	body, _, code := c.do("POST", "/admin/tick", nil)
	if code != 200 {
		c.t.Fatalf("tick: %d %v", code, body)
	}
	return body
}

func status(c *client, name string) map[string]any {
	body, _, code := c.do("GET", "/api/v1/workloads/"+name, nil)
	if code != 200 {
		c.t.Fatalf("status: %d %v", code, body)
	}
	return body
}

func events(c *client, name string) []any {
	body, _, code := c.do("GET", "/api/v1/workloads/"+name+"/events", nil)
	if code != 200 {
		c.t.Fatalf("events: %d %v", code, body)
	}
	evs, _ := body["events"].([]any)
	return evs
}

func releaseByID(c *client, id string) map[string]any {
	body, _, code := c.do("GET", "/api/v1/releases/"+id, nil)
	if code != 200 {
		c.t.Fatalf("get release: %d %v", code, body)
	}
	return body
}

func intField(m map[string]any, key string) int {
	v, ok := m[key].(float64)
	if !ok {
		return 0
	}
	return int(v)
}

func strField(m map[string]any, key string) string {
	v, _ := m[key].(string)
	return v
}

// driveUntil ticks until the named release reaches a terminal state or max
// ticks; it returns the final release JSON.
func driveUntilTerminal(t *testing.T, c *client, id string, max int) map[string]any {
	t.Helper()
	for i := 0; i < max; i++ {
		tick(c)
		r := releaseByID(c, id)
		st := strField(r, "state")
		if st == "succeeded" || st == "failed" {
			return r
		}
	}
	t.Fatalf("release %s did not finish within %d ticks", id, max)
	return nil
}

// settle ticks until the workload reports replicas live AND available.
func settle(t *testing.T, c *client, name string, replicas int) {
	t.Helper()
	for i := 0; i < 80; i++ {
		tick(c)
		s := status(c, name)
		if intField(s, "live") == replicas && intField(s, "available") == replicas {
			return
		}
	}
	t.Fatalf("workload %s never settled to %d replicas", name, replicas)
}
