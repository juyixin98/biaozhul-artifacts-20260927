package ribd_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/opp221/ribd/internal/diag"
	"github.com/opp221/ribd/internal/rib"
	"github.com/opp221/ribd/internal/server"
	"github.com/opp221/ribd/internal/store"
)

type env struct {
	*httptest.Server
}

func newEnv(t *testing.T, withSQLite bool) (*env, *store.Store) {
	t.Helper()
	var st *store.Store
	var p rib.Persister
	if withSQLite {
		s, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "rib.db"))
		if err != nil {
			t.Fatal(err)
		}
		st, p = s, s
	}
	tbl := rib.NewTable(3, p)
	lg := diag.NewLogger(&bytes.Buffer{})
	t.Cleanup(lg.Close)
	srv := httptest.NewServer(server.New(tbl, st, lg).Handler())
	t.Cleanup(srv.Close)
	return &env{Server: srv}, st
}

func (e *env) post(t *testing.T, path, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(e.URL+path, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var m map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&m)
	if rid := resp.Header.Get("X-Request-ID"); rid == "" {
		t.Error("response missing X-Request-ID")
	}
	return resp.StatusCode, m
}

func (e *env) get(t *testing.T, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(e.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var m map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&m)
	return resp.StatusCode, m
}

func TestLookupAndBatchLifecycle(t *testing.T) {
	e, _ := newEnv(t, false)

	code, m := e.get(t, "/v1/lookup?target=192.0.2.1")
	if code != 200 || m["status"] != "indeterminate" || m["failure"] != "no_route" {
		t.Fatalf("empty lookup: %d %v", code, m)
	}

	body := `{"changes":[
	  {"kind":"upsert","route":{"id":"d","prefix":"0.0.0.0/0","admin_distance":10,"next_hop":{"addr":"203.0.113.1"}}},
	  {"kind":"upsert","route":{"id":"p","prefix":"203.0.113.0/24","admin_distance":0,"next_hop":{"interface":"eth1"}}}
	]}`
	if code, m := e.post(t, "/v1/batches", body); code != 200 || m["version"].(float64) != 1 {
		t.Fatalf("apply: %d %v", code, m)
	}

	code, m = e.get(t, "/v1/lookup?target=192.0.2.5")
	if code != 200 || m["status"] != "resolved" || m["egress"] != "eth1" || m["chosen_route"] != "d" {
		t.Fatalf("recursive lookup: %v", m)
	}
	chain := m["resolve_chain"].([]any)
	if len(chain) != 2 {
		t.Fatalf("resolve chain = %v", chain)
	}
}

func TestRejectedBatchDoesNotAdvanceVersion(t *testing.T) {
	e, _ := newEnv(t, false)
	good := `{"changes":[{"kind":"upsert","route":{"id":"a","prefix":"10.0.0.0/8","admin_distance":1,"next_hop":{"interface":"eth0"}}}]}`
	if code, _ := e.post(t, "/v1/batches", good); code != 200 {
		t.Fatal("good batch failed")
	}
	bad := `{"changes":[{"kind":"upsert","route":{"id":"b","prefix":"10.0.0.0/8","admin_distance":1,"next_hop":{}}}]}`
	code, m := e.post(t, "/v1/batches", bad)
	if code != http.StatusUnprocessableEntity || m["error"] != "batch_rejected" {
		t.Fatalf("reject: %d %v", code, m)
	}
	if _, m := e.get(t, "/v1/version"); m["version"].(float64) != 1 {
		t.Fatalf("version advanced on failure: %v", m)
	}
}

func TestDryRunDoesNotMutate(t *testing.T) {
	e, _ := newEnv(t, false)
	body := `{"dry_run":true,"changes":[{"kind":"upsert","route":{"id":"a","prefix":"10.0.0.0/8","admin_distance":1,"next_hop":{"interface":"eth0"}}}]}`
	if code, _ := e.post(t, "/v1/batches", body); code != 200 {
		t.Fatal("dry run failed")
	}
	if _, m := e.get(t, "/v1/version"); m["version"].(float64) != 0 {
		t.Fatalf("dry run mutated state: %v", m)
	}
}

func TestMalformedTargetAndRequestID(t *testing.T) {
	e, _ := newEnv(t, false)
	code, m := e.get(t, "/v1/lookup?target=not-an-ip")
	if code != 200 || m["status"] != "rejected" || m["failure"] != "bad_query" {
		t.Fatalf("bad query: %v", m)
	}

	// Client-supplied request id is echoed and reused.
	req, _ := http.NewRequest(http.MethodGet, e.URL+"/v1/version", nil)
	req.Header.Set("X-Request-ID", "req-fixed-123")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.Header.Get("X-Request-ID") != "req-fixed-123" {
		t.Fatal("client request id not honored")
	}
}

func TestEventsAndReplayOverHTTP(t *testing.T) {
	e, st := newEnv(t, true)
	defer st.Close()
	body := `{"changes":[
	  {"kind":"upsert","route":{"id":"d","prefix":"0.0.0.0/0","admin_distance":10,"next_hop":{"interface":"eth0"}}}
	]}`
	if code, _ := e.post(t, "/v1/batches", body); code != 200 {
		t.Fatal("apply failed")
	}
	code, m := e.get(t, "/v1/events")
	if code != 200 {
		t.Fatalf("events: %d", code)
	}
	if m["count"].(float64) != 1 {
		t.Fatalf("events count = %v", m["count"])
	}
	code, m = e.post(t, "/v1/replay", `{"verify":true}`)
	if code != 200 {
		t.Fatalf("replay: %d %v", code, m)
	}
	verify := m["verify"].(map[string]any)
	if verify["ok"] != true {
		t.Fatalf("replay verification failed: %v", verify)
	}
	if m["replayed_version"].(float64) != 1 {
		t.Fatalf("replayed version = %v", m["replayed_version"])
	}
}

func TestSensitiveMetadataRedactedInRoutes(t *testing.T) {
	e, _ := newEnv(t, false)
	body := `{"changes":[{"kind":"upsert","route":{"id":"s","prefix":"10.0.0.0/8","admin_distance":1,
	  "next_hop":{"interface":"eth0"},"meta":{"token":"TOPSECRET","note":"ok"}}}]}`
	if code, _ := e.post(t, "/v1/batches", body); code != 200 {
		t.Fatal("apply failed")
	}
	_, m := e.get(t, "/v1/routes")
	raw, _ := json.Marshal(m)
	if strings.Contains(string(raw), "TOPSECRET") {
		t.Fatal("sensitive token leaked via /v1/routes")
	}
	if !strings.Contains(string(raw), "[REDACTED]") {
		t.Fatal("redaction marker missing")
	}
}
