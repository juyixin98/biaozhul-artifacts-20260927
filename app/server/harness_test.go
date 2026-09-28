package server

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync/atomic"
	"testing"

	"replicactl/app/store"
	"replicactl/core/controller"
	"replicactl/core/model"
)

// harness wires the real HTTP stack against a temp SQLite file with a
// controllable synthetic clock. It exercises the full production wiring; only
// the clock and transport are fake.
type harness struct {
	t       *testing.T
	srv     *Server
	client  *http.Client
	baseURL string
	tick    atomic.Int64
	logs    bytes.Buffer
}

func newHarness(t *testing.T, initialReplicas int) *harness {
	t.Helper()
	dir := t.TempDir()
	dsn := "file:" + filepath.Join(dir, "harness.db")
	db, err := store.Open(dsn)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { db.Close() })

	cfg := model.DefaultConfig()
	fx, err := store.NewDurableFixture(db, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if initialReplicas > 0 {
		if err := fx.SetReplicas(initialReplicas); err != nil {
			t.Fatal(err)
		}
	}
	decisions := store.NewDecisionLog(db)
	history := store.NewRawPointLog(db)
	faultyD := &FaultyDecisionStore{Inner: decisions}
	faultyH := &FaultyHistory{Inner: history}

	logger := log.New(io.Discard, "", 0)
	ctl, err := controller.New(cfg, fx, fx, faultyD, faultyH)
	if err != nil {
		t.Fatal(err)
	}
	srv := New(ctl, fx, faultyD, faultyH, logger)
	srv.RawDecisions = decisions
	srv.FaultyDecisions = faultyD
	srv.FaultyHistory = faultyH

	h := &harness{t: t, srv: srv, client: &http.Client{}}
	h.tick.Store(100000)
	clock := func() int64 { return h.tick.Load() }
	srv.Clock = clock
	fx.Clock = clock

	ts := httptest.NewServer(srv.Handler())
	h.baseURL = ts.URL
	t.Cleanup(ts.Close)
	return h
}

func (h *harness) setTick(v int64) { h.tick.Store(v) }

func (h *harness) doRaw(method, path, rid, rawBody string) (int, map[string]any) {
	h.t.Helper()
	req, err := http.NewRequest(method, h.baseURL+path, bytes.NewReader([]byte(rawBody)))
	if err != nil {
		h.t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if rid != "" {
		req.Header.Set("X-Request-ID", rid)
	}
	resp, err := h.client.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func (h *harness) do(method, path, rid string, body any) (int, map[string]any) {
	h.t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, h.baseURL+path, rdr)
	if err != nil {
		h.t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if rid != "" {
		req.Header.Set("X-Request-ID", rid)
	}
	resp, err := h.client.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func (h *harness) report(id string, load float64, at int64) {
	h.t.Helper()
	code, body := h.do("POST", "/v1/metrics", "metric-"+id,
		map[string]any{"instance_id": id, "load": load, "reported_at": at})
	if code != http.StatusAccepted {
		h.t.Fatalf("report %s: status %d body %v", id, code, body)
	}
}

func (h *harness) demand(present bool, at int64) {
	h.t.Helper()
	code, body := h.do("POST", "/v1/demand", "demand",
		map[string]any{"present": present, "reported_at": at})
	if code != http.StatusAccepted {
		h.t.Fatalf("demand: status %d body %v", code, body)
	}
}

func (h *harness) reconcile(rid string) (int, map[string]any) {
	return h.do("POST", "/v1/reconcile", rid, map[string]any{})
}

func getString(m map[string]any, key string) string {
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}

func getInt(m map[string]any, key string) int {
	switch v := m[key].(type) {
	case float64:
		return int(v)
	case int:
		return v
	}
	return 0
}
