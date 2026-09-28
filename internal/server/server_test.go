package server_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"natlab/internal/config"
	"natlab/internal/memstore"
	"natlab/internal/nat"
	"natlab/internal/server"
)

func newTestServer(t *testing.T) (*httptest.Server, *nat.Engine) {
	t.Helper()
	cfg := config.Defaults()
	cfg.PortLow = 40000
	cfg.PortHigh = 40002
	cfg.Resolve()
	eng := nat.NewEngine(cfg, memstore.New())
	ts := httptest.NewServer(server.New(eng).Handler())
	t.Cleanup(ts.Close)
	return ts, eng
}

func postJSON(t *testing.T, url string, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(url, "application/json", bytes.NewBufferString(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return resp.StatusCode, out
}

func TestHTTPEvaluate_AcceptAndTranslate(t *testing.T) {
	ts, _ := newTestServer(t)
	body := `{"ts":"2026-01-01T00:00:00Z","src_ip":"10.0.0.10","src_port":5001,"dst_ip":"198.51.100.1","dst_port":53,"protocol":"UDP","direction":"outbound"}`
	status, out := postJSON(t, ts.URL+"/runs/demo/packets", body)
	if status != http.StatusOK {
		t.Fatalf("status=%d body=%v", status, out)
	}
	if out["accepted"] != true {
		t.Fatalf("accepted=%v", out["accepted"])
	}
	if mp, _ := out["mapped_port"].(float64); mp != 40000 {
		t.Fatalf("mapped_port=%v", out["mapped_port"])
	}
	tr := out["translated"].(map[string]any)
	if tr["src_ip"] != "203.0.113.1" || tr["src_port"].(float64) != 40000 {
		t.Fatalf("translated=%v", tr)
	}
}

func TestHTTPRejectionCategories(t *testing.T) {
	ts, _ := newTestServer(t)
	// invalid_input: bad JSON -> 400.
	resp, err := http.Post(ts.URL+"/runs/demo/packets", "application/json", strings.NewReader("{not json"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad json status=%d, want 400", resp.StatusCode)
	}
	// state_conflict: inbound to unowned port -> 200 with rejected verdict.
	body := `{"ts":"2026-01-01T00:00:00Z","src_ip":"198.51.100.9","src_port":80,"dst_ip":"203.0.113.1","dst_port":55555,"protocol":"UDP","direction":"inbound"}`
	status, out := postJSON(t, ts.URL+"/runs/demo/packets", body)
	if status != http.StatusOK || out["code"] != "INBOUND_NO_MAPPING" || out["category"] != "state_conflict" {
		t.Fatalf("status=%d out=%v", status, out)
	}
}

func TestHTTPEventsAndMappings(t *testing.T) {
	ts, _ := newTestServer(t)
	postJSON(t, ts.URL+"/runs/log/packets",
		`{"ts":"2026-01-01T00:00:00Z","src_ip":"10.0.0.10","src_port":5001,"dst_ip":"198.51.100.1","dst_port":53,"protocol":"UDP","direction":"outbound"}`)
	postJSON(t, ts.URL+"/runs/log/packets",
		`{"ts":"2026-01-01T00:00:01Z","src_ip":"198.51.100.1","src_port":53,"dst_ip":"203.0.113.1","dst_port":40000,"protocol":"UDP","direction":"inbound"}`)

	resp, err := http.Get(ts.URL + "/runs/log/events")
	if err != nil {
		t.Fatal(err)
	}
	var events []map[string]any
	json.NewDecoder(resp.Body).Decode(&events)
	resp.Body.Close()
	if len(events) != 2 || events[0]["accepted"] != true {
		t.Fatalf("events=%v", events)
	}
	if pkt := events[0]["packet"].(map[string]any); pkt["src_ip"] != "10.0.0.10" {
		t.Fatalf("event packet round-trip=%v", pkt)
	}

	resp2, err := http.Get(ts.URL + "/runs/log/mappings?active=true")
	if err != nil {
		t.Fatal(err)
	}
	var maps []map[string]any
	json.NewDecoder(resp2.Body).Decode(&maps)
	resp2.Body.Close()
	if len(maps) != 1 || maps[0]["mapped_port"].(float64) != 40000 || maps[0]["state"] != "open" {
		t.Fatalf("mappings=%v", maps)
	}
}

func TestHTTPBatch(t *testing.T) {
	ts, _ := newTestServer(t)
	batch := `[
	  {"ts":"2026-01-01T00:00:00Z","src_ip":"10.0.0.10","src_port":6001,"dst_ip":"198.51.100.1","dst_port":53,"protocol":"UDP","direction":"outbound"},
	  {"ts":"2026-01-01T00:00:01Z","src_ip":"10.0.0.10","src_port":6002,"dst_ip":"198.51.100.2","dst_port":53,"protocol":"UDP","direction":"outbound"}
	]`
	resp, err := http.Post(ts.URL+"/runs/b/packets/batch", "application/json", strings.NewReader(batch))
	if err != nil {
		t.Fatal(err)
	}
	var out []map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	resp.Body.Close()
	if len(out) != 2 || out[0]["mapped_port"].(float64) != 40000 || out[1]["mapped_port"].(float64) != 40001 {
		t.Fatalf("batch=%v", out)
	}
}

func TestHealthz(t *testing.T) {
	ts, _ := newTestServer(t)
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz=%d", resp.StatusCode)
	}
}
