package service_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"cidrsvc/internal/config"
	"cidrsvc/internal/service"
	"cidrsvc/internal/store"
)

type testEnv struct {
	svc    *service.Service
	store  *store.SQLiteStore
	logs   *memLogger
	server *httptest.Server
}

type memLogger struct {
	entries []service.LogEntry
}

func (m *memLogger) Log(e service.LogEntry) { m.entries = append(m.entries, e) }

func newEnv(t *testing.T) *testEnv {
	t.Helper()
	cfg := config.Default()
	cfg.MaxInputPrefixes = 10
	st, err := store.Open(context.Background(),
		"file:servicetest_"+strings.ReplaceAll(t.Name(), "/", "_")+"?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	lg := &memLogger{}
	svc := service.New(cfg, st, lg)
	srv := httptest.NewServer(svc.Routes())
	t.Cleanup(srv.Close)
	return &testEnv{svc: svc, store: st, logs: lg, server: srv}
}

func post(t *testing.T, url string, body string, headers map[string]string) (int, map[string]any, string) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, url+"/v1/compute", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf := new(bytes.Buffer)
	buf.ReadFrom(resp.Body)
	var m map[string]any
	_ = json.Unmarshal(buf.Bytes(), &m)
	return resp.StatusCode, m, resp.Header.Get("X-Request-Id")
}

func get(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var m map[string]any
	buf := new(bytes.Buffer)
	buf.ReadFrom(resp.Body)
	_ = json.Unmarshal(buf.Bytes(), &m)
	return resp.StatusCode, m
}

func TestComputeSuccessAndProof(t *testing.T) {
	env := newEnv(t)
	body := `{"allow":["10.0.0.0/24"],"exclude":["10.0.0.128/25"]}`
	status, m, reqID := post(t, env.server.URL, body, nil)
	if status != http.StatusOK {
		t.Fatalf("status=%d body=%v", status, m)
	}
	if reqID == "" || m["request_id"] != reqID {
		t.Fatalf("correlation id mismatch: header=%q body=%v", reqID, m["request_id"])
	}
	prefixes, _ := json.Marshal(m["prefixes"])
	if string(prefixes) != `["10.0.0.0/25"]` {
		t.Fatalf("prefixes=%s", prefixes)
	}
	proof := m["proof"].(map[string]any)
	if proof["exactly_equivalent"] != true {
		t.Fatalf("proof=%v", proof)
	}
	if proof["target_address_count"] != proof["cover_address_count"] {
		t.Fatalf("address counts: %v", proof)
	}
	steps := m["steps"].([]any)
	if len(steps) != 7 {
		t.Fatalf("want 7 explained steps, got %d", len(steps))
	}
	for _, st := range steps {
		sm := st.(map[string]any)
		if sm["location"] == "" {
			t.Fatalf("step missing location: %v", sm)
		}
	}
	if m["version"] == nil || m["version"] == "" {
		t.Fatal("version missing")
	}
}

func TestClientRequestIDIsHonored(t *testing.T) {
	env := newEnv(t)
	_, m, hdr := post(t, env.server.URL, `{"allow":["0.0.0.0/0"]}`,
		map[string]string{"X-Request-Id": "corr-123", "X-Client-Ref": "ticket-9"})
	if hdr != "corr-123" || m["request_id"] != "corr-123" {
		t.Fatalf("client request id not honored: %q %v", hdr, m["request_id"])
	}
	// Replay uses the same id.
	status, got := get(t, env.server.URL+"/v1/requests/corr-123")
	if status != http.StatusOK || got["client_ref"] != "ticket-9" {
		t.Fatalf("replay: status=%d %v", status, got)
	}
}

func TestComputeErrorCategories(t *testing.T) {
	env := newEnv(t)
	cases := []struct {
		name     string
		body     string
		wantCode string
		wantHTTP int
	}{
		{"malformed", `{"allow":["not-a-cidr"]}`, "malformed_cidr", 400},
		{"no slash", `{"allow":["10.0.0.0"]}`, "malformed_cidr", 400},
		{"prefix too long", `{"allow":["10.0.0.0/40"]}`, "prefix_length_too_long", 400},
		{"host bits strict default lenient", `{"allow":["10.0.0.5/24"],"strict":true}`, "host_bits_present", 400},
		{"family mismatch", `{"allow":["10.0.0.0/8"],"exclude":["2001:db8::/32"]}`, "family_mismatch", 400},
		{"invalid json", `{"allow":[}`, "invalid_json", 400},
		{"too many prefixes", `{"allow":["0.0.0.0/0","1.0.0.0/8","2.0.0.0/8","3.0.0.0/8","4.0.0.0/8","5.0.0.0/8","6.0.0.0/8","7.0.0.0/8","8.0.0.0/8","9.0.0.0/8","10.0.0.0/8"]}`, "too_many_prefixes", 400},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			status, m, _ := post(t, env.server.URL, c.body, nil)
			if status != c.wantHTTP {
				t.Fatalf("http=%d want %d body=%v", status, c.wantHTTP, m)
			}
			if m["error_code"] != c.wantCode {
				t.Fatalf("code=%v want %s (error=%v)", m["error_code"], c.wantCode, m["error"])
			}
			if m["location"] == "" || m["request_id"] == "" {
				t.Fatalf("error body not explainable: %v", m)
			}
		})
	}
}

func TestLenientHostBitsProducesWarning(t *testing.T) {
	env := newEnv(t)
	status, m, _ := post(t, env.server.URL, `{"allow":["10.0.0.5/24"]}`, nil)
	if status != 200 {
		t.Fatalf("%v", m)
	}
	warns := m["warnings"].([]any)
	if len(warns) == 0 || !strings.Contains(warns[0].(string), "canonicalized") {
		t.Fatalf("warnings=%v", warns)
	}
}

func TestReplayEndpoints(t *testing.T) {
	env := newEnv(t)
	post(t, env.server.URL, `{"allow":["192.168.0.0/16"]}`, map[string]string{"X-Request-Id": "r-ok"})
	post(t, env.server.URL, `{"allow":["garbage"]}`, map[string]string{"X-Request-Id": "r-err"})

	status, one := get(t, env.server.URL+"/v1/requests/r-ok")
	if status != 200 || one["status"] != "ok" {
		t.Fatalf("%v", one)
	}
	status, bad := get(t, env.server.URL+"/v1/requests/r-err")
	if status != 200 || bad["status"] != "error" || bad["error_code"] != "malformed_cidr" {
		t.Fatalf("error record replay: %v", bad)
	}
	if _, ok := bad["steps"]; ok {
		t.Fatal("failed request should not carry success steps")
	}

	status, missing := get(t, env.server.URL+"/v1/requests/unknown")
	if status != 404 || missing["error_code"] != "not_found" {
		t.Fatalf("missing: %d %v", status, missing)
	}

	status, list := get(t, env.server.URL+"/v1/requests?status=error")
	if status != 200 || list["count"].(float64) != 1 {
		t.Fatalf("list error filter: %v", list)
	}

	status, stats := get(t, env.server.URL+"/v1/stats")
	if status != 200 {
		t.Fatalf("stats: %v", stats)
	}
	counts := stats["status_counts"].(map[string]any)
	if counts["ok"].(float64) != 1 || counts["error"].(float64) != 1 {
		t.Fatalf("counts=%v", counts)
	}
}

func TestFullSpaceAndEmptySet(t *testing.T) {
	env := newEnv(t)
	status, full, _ := post(t, env.server.URL, `{"allow":["::/0"]}`, nil)
	if status != 200 {
		t.Fatalf("%v", full)
	}
	if p, _ := json.Marshal(full["prefixes"]); string(p) != `["::/0"]` {
		t.Fatalf("full v6 = %s", p)
	}

	status, empty, _ := post(t, env.server.URL, `{"allow":[],"exclude":[]}`, nil)
	if status != 200 || empty["empty"] != true {
		t.Fatalf("empty set: %v", empty)
	}
	if p, _ := json.Marshal(empty["prefixes"]); string(p) != `[]` {
		t.Fatalf("empty prefixes = %s", p)
	}
}

func TestOversizedBodyRejected(t *testing.T) {
	env := newEnv(t)
	big := strings.Repeat("x", 5<<20)
	status, m, _ := post(t, env.server.URL, `{"allow":["`+big+`"]}`, nil)
	if status != 400 || m["error_code"] != "invalid_json" {
		t.Fatalf("status=%d body=%v", status, m)
	}
}

func TestRequestAppearsInLogs(t *testing.T) {
	env := newEnv(t)
	post(t, env.server.URL, `{"allow":["10.0.0.0/24"]}`, map[string]string{"X-Request-Id": "log-1"})
	time.Sleep(10 * time.Millisecond)
	found := false
	for _, e := range env.logs.entries {
		if e.RequestID == "log-1" && e.Message == "http_access" {
			found = true
		}
	}
	if !found {
		t.Fatalf("no correlated access log; entries=%d", len(env.logs.entries))
	}
}

func TestHealth(t *testing.T) {
	env := newEnv(t)
	status, m := get(t, env.server.URL+"/healthz")
	if status != 200 || m["status"] != "ok" {
		t.Fatalf("%d %v", status, m)
	}
}
