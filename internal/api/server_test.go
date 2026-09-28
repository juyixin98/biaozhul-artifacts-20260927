package api

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"rib/internal/config"
	"rib/internal/netmodel"
	"rib/internal/rib"
	"rib/internal/store"
)

func newTestServer(t *testing.T) (*Server, http.Handler) {
	t.Helper()
	// 内存库文件名用测试名（URI 中不含路径分隔符，"/" 替换为 "_"）。
	dsn := "file:" + strings.ReplaceAll(t.Name(), "/", "_") + "?mode=memory&cache=shared"
	st, err := store.Open(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })

	cfg := config.Defaults()
	cfg.SQLiteDSN = dsn
	cfg.RedactDiag = true
	r := rib.New().WithMaxDepth(cfg.MaxDepth)
	srv := &Server{
		RIB: r, Store: st, Cfg: cfg,
		Logger: slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil)),
	}
	return srv, srv.Routes()
}

func doJSON(t *testing.T, h http.Handler, method, path string, body any, rid string) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if rid != "" {
		req.Header.Set("X-Request-ID", rid)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("non-json response %q: %v", rec.Body.String(), err)
		}
	}
	return rec.Code, out
}

func postRoute(t *testing.T, h http.Handler, body string, rid string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/routes", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if rid != "" {
		req.Header.Set("X-Request-ID", rid)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func TestHealthAndRequestID(t *testing.T) {
	_, h := newTestServer(t)
	status, body := doJSON(t, h, "GET", "/healthz", nil, "rid-health")
	if status != 200 {
		t.Fatalf("status=%d body=%v", status, body)
	}
	if body["request_id"] != "rid-health" {
		t.Fatalf("request_id not echoed: %v", body["request_id"])
	}
	if _, ok := body["version"]; !ok {
		t.Fatal("health must expose table version")
	}

	// 未提供 X-Request-ID 时必须生成。
	_, body2 := doJSON(t, h, "GET", "/healthz", nil, "")
	rid, _ := body2["request_id"].(string)
	if !strings.HasPrefix(rid, "req-") {
		t.Fatalf("generated request id wrong: %q", rid)
	}
}

func TestUpsertNormalizesPrefix(t *testing.T) {
	_, h := newTestServer(t)
	body := `{"id":"e1","prefix":"203.0.113.0/24","admin_distance":0,"metric":0,"protocol":"connected",
		"nexthop":{"kind":"connected","interface":"eth0"}}`
	status, resp := postRoute(t, h, body, "rid-up")
	if status != http.StatusCreated {
		t.Fatalf("status=%d resp=%v", status, resp)
	}
	data := resp["data"].(map[string]any)
	rt := data["route"].(map[string]any)
	if rt["prefix"] != "203.0.113.0/24" {
		t.Fatalf("prefix=%v", rt["prefix"])
	}
	if resp["request_id"] != "rid-up" {
		t.Fatal("request id lost")
	}

	// 非规范主机位前缀：接受但返回规范化形式（/16 保留 10.10，主机位清零）。
	body2 := `{"id":"e2","prefix":"10.10.10.255/16","admin_distance":5,"metric":0,"protocol":"static",
		"nexthop":{"kind":"address","address":"203.0.113.5"}}`
	_, resp2 := postRoute(t, h, body2, "")
	rt2 := resp2["data"].(map[string]any)["route"].(map[string]any)
	if rt2["prefix"] != "10.10.0.0/16" {
		t.Fatalf("expected canonical 10.10.0.0/16, got %v", rt2["prefix"])
	}
}

func TestValidationErrorCategories(t *testing.T) {
	_, h := newTestServer(t)

	cases := []struct {
		name     string
		body     string
		wantCode string
		status   int
	}{
		{"bad json", `{not-json`, "bad_json", 400},
		{"unknown field", `{"id":"x","bogus":1}`, "bad_json", 400},
		{"bad prefix", `{"id":"x","prefix":"10.0.0.0/40","nexthop":{"kind":"blackhole"}}`, "invalid_prefix", 400},
		{"af mismatch", `{"id":"x","prefix":"10.0.0.0/8","admin_distance":1,
			"nexthop":{"kind":"address","address":"2001:db8::1"}}`, "address_family_mismatch", 400},
		{"blackhole with address", `{"id":"x","prefix":"10.0.0.0/8","admin_distance":1,
			"nexthop":{"kind":"blackhole","address":"10.0.0.9"}}`, "bad_nexthop", 400},
		{"bad distance", `{"id":"x","prefix":"10.0.0.0/8","admin_distance":999,
			"nexthop":{"kind":"blackhole"}}`, "bad_route_attribute", 400},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			status, resp := postRoute(t, h, c.body, "")
			if status != c.status {
				t.Fatalf("http status=%d want %d resp=%v", status, c.status, resp)
			}
			errObj := resp["error"].(map[string]any)
			if errObj["code"] != c.wantCode {
				t.Fatalf("error code=%v want %s", errObj["code"], c.wantCode)
			}
			if diags, _ := errObj["diag"].([]any); len(diags) == 0 {
				t.Fatal("rejection must carry diagnostics explaining why")
			}
		})
	}
}

func TestLookupChainAndRedaction(t *testing.T) {
	_, h := newTestServer(t)
	mustPost := func(body string) {
		t.Helper()
		if s, r := postRoute(t, h, body, "seed"); s != 201 {
			t.Fatalf("seed failed %d %v", s, r)
		}
	}
	mustPost(`{"id":"edge","prefix":"203.0.113.0/24","admin_distance":0,"protocol":"connected",
		"nexthop":{"kind":"connected","interface":"eth0"}}`)
	mustPost(`{"id":"def","prefix":"0.0.0.0/0","admin_distance":255,"protocol":"static",
		"nexthop":{"kind":"address","address":"203.0.113.1"}}`)

	status, resp := doJSON(t, h, "GET", "/v1/lookup?target=198.51.100.77", nil, "rid-lk")
	if status != 200 {
		t.Fatalf("%d %v", status, resp)
	}
	data := resp["data"].(map[string]any)
	if data["status"] != "forwarded" || data["egress"] != "eth0" {
		t.Fatalf("lookup data=%v", data)
	}
	chain := data["chain"].([]any)
	if len(chain) != 2 {
		t.Fatalf("chain len=%d", len(chain))
	}
	diags, _ := data["diagnostics"].([]any)
	if len(diags) == 0 {
		t.Fatal("lookup must include diagnostics")
	}
	// 最终结论（脱敏）也必须出现在响应诊断中。
	hasSummary := false
	for _, x := range diags {
		if s, _ := x.(string); strings.Contains(s, "lookup") && strings.Contains(s, "forwarded") {
			hasSummary = true
		}
	}
	if !hasSummary {
		t.Fatalf("diagnostics missing final summary: %v", diags)
	}
	// 脱敏开启：诊断中不得出现完整目标地址 198.51.100.77。
	for _, d := range diags {
		if strings.Contains(d.(string), "198.51.100.77") {
			t.Fatalf("sensitive target leaked in diag: %v", d)
		}
	}

	// 非法目标地址。
	status, resp = doJSON(t, h, "GET", "/v1/lookup?target=nonsense", nil, "")
	if status != 400 || resp["error"].(map[string]any)["code"] != "invalid_address" {
		t.Fatalf("bad target: %d %v", status, resp)
	}
}

func TestReplaceAtomicAndList(t *testing.T) {
	_, h := newTestServer(t)
	// 初始一条。
	postRoute(t, h, `{"id":"old","prefix":"10.0.0.0/8","admin_distance":1,
		"nexthop":{"kind":"blackhole"}}`, "")

	bulk := map[string]any{
		"v4": []map[string]any{
			{"id": "new1", "prefix": "192.0.2.0/24", "admin_distance": 0, "protocol": "connected",
				"nexthop": map[string]any{"kind": "connected", "interface": "eth9"}},
		},
		"v6": []map[string]any{
			{"id": "new6", "prefix": "2001:db8::/32", "admin_distance": 1,
				"nexthop": map[string]any{"kind": "blackhole"}},
		},
	}
	status, resp := doJSON(t, h, "POST", "/v1/routes/replace", bulk, "rid-bulk")
	if status != 200 {
		t.Fatalf("replace %d %v", status, resp)
	}
	// 一次替换只产生一条事件、一次版本推进。
	data := resp["data"].(map[string]any)
	if data["event_seq"].(float64) != 2 { // seed upsert=1, replace=2
		t.Fatalf("event_seq=%v", data["event_seq"])
	}

	status, body := doJSON(t, h, "GET", "/v1/routes?family=ipv4", nil, "")
	routes := body["data"].(map[string]any)["routes"].([]any)
	if len(routes) != 1 {
		t.Fatalf("after replace v4 routes=%v", routes)
	}
	status, body = doJSON(t, h, "GET", "/v1/routes?family=ipv6", nil, "")
	routes = body["data"].(map[string]any)["routes"].([]any)
	if len(routes) != 1 {
		t.Fatalf("after replace v6 routes=%v", routes)
	}

	// 非法批量整体拒绝：旧表仍可查。
	badBulk := map[string]any{
		"v4": []map[string]any{
			{"id": "broken", "prefix": "10.0.0.0/8", "admin_distance": 1,
				"nexthop": map[string]any{"kind": "address", "address": "2001:db8::1"}},
		},
	}
	status, _ = doJSON(t, h, "POST", "/v1/routes/replace", badBulk, "")
	if status != 400 {
		t.Fatalf("bad bulk status=%d", status)
	}
	status, body = doJSON(t, h, "GET", "/v1/lookup?target=192.0.2.9", nil, "")
	if status != 200 || body["data"].(map[string]any)["egress"] != "eth9" {
		t.Fatalf("table not preserved after rejected replace: %v", body)
	}
}

func TestDeleteAnd404(t *testing.T) {
	_, h := newTestServer(t)
	postRoute(t, h, `{"id":"d1","prefix":"10.0.0.0/8","admin_distance":1,
		"nexthop":{"kind":"blackhole"}}`, "")

	status, _ := doJSON(t, h, "DELETE", "/v1/routes?prefix=10.0.0.0/8&id=d1", nil, "")
	if status != 200 {
		t.Fatalf("delete status=%d", status)
	}
	status, resp := doJSON(t, h, "DELETE", "/v1/routes?prefix=10.0.0.0/8&id=d1", nil, "")
	if status != 404 || resp["error"].(map[string]any)["code"] != "not_found" {
		t.Fatalf("second delete: %d %v", status, resp)
	}
}

func TestEventsAndReplayConsistency(t *testing.T) {
	_, h := newTestServer(t)
	postRoute(t, h, `{"id":"a","prefix":"10.0.0.0/8","admin_distance":1,
		"nexthop":{"kind":"blackhole"}}`, "ev1")
	postRoute(t, h, `{"id":"b","prefix":"192.168.0.0/16","admin_distance":0,"protocol":"connected",
		"nexthop":{"kind":"connected","interface":"eth1"}}`, "ev2")

	status, body := doJSON(t, h, "GET", "/v1/events", nil, "")
	if status != 200 {
		t.Fatalf("events %d", status)
	}
	events := body["data"].(map[string]any)["events"].([]any)
	if len(events) != 2 {
		t.Fatalf("events=%v", events)
	}

	// 回放必须与当前表一致。
	status, body = doJSON(t, h, "POST", "/v1/replay", map[string]any{}, "")
	if status != 200 {
		t.Fatalf("replay status=%d body=%v", status, body)
	}
	rep := body["data"].(map[string]any)
	if rep["consistent"] != true {
		t.Fatalf("replay not consistent: %v", rep["mismatches"])
	}
	if rep["events_played"].(float64) != 2 {
		t.Fatalf("events_played=%v", rep["events_played"])
	}
}

func TestPersistenceAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	dsn := "file:" + dir + "/rib.db?cache=shared"

	ctx := context.Background()
	st1, err := store.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	r1 := rib.New()
	rt := netmodel.Route{
		ID: "persist", Prefix: netmodel.MustPrefix("100.64.0.0/10"),
		AdminDistance: 1, Nexthop: netmodel.Nexthop{Kind: netmodel.NHBlackhole},
	}
	if err := r1.Upsert(rt); err != nil {
		t.Fatal(err)
	}
	if _, err := st1.CommitUpsert(ctx, rt, r1.Version(), "x"); err != nil {
		t.Fatal(err)
	}
	st1.Close()

	// 重新打开：快照表装载恢复路由。
	st2, err := store.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	loaded, err := st2.LoadRoutes(ctx)
	if err != nil || len(loaded) != 1 || loaded[0].ID != "persist" {
		t.Fatalf("reload routes=%v err=%v", loaded, err)
	}
}
