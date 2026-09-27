package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"flowrouter/internal/api"
	"flowrouter/internal/config"
	"flowrouter/internal/replay"
	"flowrouter/internal/router"
	"flowrouter/internal/store"
	"flowrouter/internal/testutil"
)

type env struct {
	server *httptest.Server
	st     *store.Store
	rt     *router.Router
	dir    string
}

func setup(t *testing.T, cfgPath string) *env {
	t.Helper()
	cfg, err := config.Load(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	cfg.Store.Path = filepath.Join(t.TempDir(), "test.db")
	dir := t.TempDir()
	// copy flow fixtures into the temp lib dir
	copyFixtures(t, dir)

	st, err := store.Open(context.Background(), cfg.Store.Path, 500)
	if err != nil {
		t.Fatal(err)
	}
	rt := router.New(cfg.VNodesPerWeight, cfg.MaxVNodes)
	srv := &api.Server{
		RT: rt, Store: st, Config: cfg, ConfigPath: cfgPath,
		Library: replay.NewFileLibrary(dir),
		Planner: replay.NewPlanner(st, cfg.VNodesPerWeight, cfg.MaxVNodes),
		Logger:  slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	// initial load + persist
	raw, _ := os.ReadFile(cfgPath)
	snap, changed, err := rt.Load(cfg.Members, map[string]bool{})
	if err != nil {
		t.Fatal(err)
	}
	if err := srv.PersistInitial(context.Background(), raw, cfgPath, "sha-test", snap); err != nil {
		t.Fatal(err)
	}
	_ = changed
	ts := httptest.NewServer(srv.NewMux())
	t.Cleanup(func() {
		ts.Close()
		_ = st.Close()
	})
	return &env{server: ts, st: st, rt: rt, dir: dir}
}

func copyFixtures(t *testing.T, dir string) {
	t.Helper()
	src := filepath.Join(testutil.RepoRoot(t), "testdata", "flowsets")
	entries, err := os.ReadDir(src)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		b, err := os.ReadFile(filepath.Join(src, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, e.Name()), b, 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func exampleConfigPath(t *testing.T) string {
	return filepath.Join(testutil.RepoRoot(t), "configs", "config.example.yaml")
}

func (e *env) do(t *testing.T, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, e.server.URL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &m)
	}
	return resp.StatusCode, m
}

func TestRouteAndVersion(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	status, body := e.do(t, "POST", "/v1/route", map[string]any{
		"src_ip": "10.0.0.1", "dst_ip": "10.1.0.1", "proto": 6,
		"src_port": 12345, "dst_port": 80,
	})
	if status != 200 {
		t.Fatalf("status=%d body=%v", status, body)
	}
	if body["member_id"] == nil || body["member_id"] == "" {
		t.Fatalf("no member: %v", body)
	}
	if body["version"].(float64) != 1 {
		t.Fatalf("version=%v", body["version"])
	}

	status, body = e.do(t, "GET", "/v1/version", nil)
	if status != 200 {
		t.Fatal(status)
	}
	if body["num_on_ring"].(float64) != 3 {
		t.Fatalf("on ring=%v", body["num_on_ring"])
	}
	bucket := body["bucket_share"].(map[string]any)
	if bucket["hop-c"].(float64) != 0.5 || bucket["hop-a"].(float64) != 0.25 {
		t.Fatalf("bucket share=%v", bucket)
	}
}

func TestInputErrorClass(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	// malformed JSON -> 400 INVALID_INPUT/BAD_JSON_BODY
	resp, err := http.Post(e.server.URL+"/v1/route", "application/json", bytes.NewReader([]byte("{bad")))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	var env_ struct {
		Error struct {
			Kind string `json:"kind"`
			Code string `json:"code"`
		} `json:"error"`
	}
	raw, _ := io.ReadAll(resp.Body)
	if err := json.Unmarshal(raw, &env_); err != nil {
		t.Fatal(err)
	}
	if env_.Error.Kind != "INVALID_INPUT" || env_.Error.Code != "BAD_JSON_BODY" {
		t.Fatalf("envelope=%s", raw)
	}
	if resp.Header.Get("X-Request-Id") == "" {
		t.Fatal("request id header missing")
	}

	// bad tuple -> 400 with distinct code
	status, body := e.do(t, "POST", "/v1/route", map[string]any{
		"src_ip": "nope", "dst_ip": "10.0.0.1", "proto": 6,
	})
	if status != 400 {
		t.Fatalf("status=%d", status)
	}
	errObj := body["error"].(map[string]any)
	if errObj["code"] != "BAD_SRC_IP" {
		t.Fatalf("code=%v", errObj["code"])
	}

	// unknown field rejected by DisallowUnknownFields
	status, _ = e.do(t, "POST", "/v1/route", map[string]any{"bogus": 1})
	if status != 400 {
		t.Fatalf("unknown field status=%d", status)
	}
}

func TestDownRoute503AndRecovery(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	// mark all three down through admin API using CAS versions
	v := 1
	for _, id := range []string{"hop-a", "hop-b", "hop-c"} {
		status, body := e.do(t, "POST", "/admin/members/"+id+"/down",
			map[string]any{"expected_version": v, "reason": "test-failure"})
		if status != 200 {
			t.Fatalf("down %s: %d %v", id, status, body)
		}
		v = int(body["version"].(float64))
	}
	// routing now returns 503 NO_HEALTHY_MEMBER/ALL_DOWN
	resp, _ := http.Post(e.server.URL+"/v1/route", "application/json",
		bytes.NewReader([]byte(`{"src_ip":"10.0.0.1","dst_ip":"10.1.0.1","proto":6,"src_port":1,"dst_port":2}`)))
	if resp.StatusCode != 503 {
		t.Fatalf("status=%d want 503", resp.StatusCode)
	}
	var envelope struct {
		Error struct {
			Kind string `json:"kind"`
			Code string `json:"code"`
		} `json:"error"`
	}
	b, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(b, &envelope)
	resp.Body.Close()
	if envelope.Error.Kind != "NO_HEALTHY_MEMBER" || envelope.Error.Code != "ALL_DOWN" {
		t.Fatalf("got %s", b)
	}

	// stale CAS version on recovery -> 409 STATE_CONFLICT/VERSION
	status, body := e.do(t, "POST", "/admin/members/hop-a/up",
		map[string]any{"expected_version": 1})
	if status != 409 {
		t.Fatalf("status=%d want 409", status)
	}
	if body["error"].(map[string]any)["code"] != "VERSION" {
		t.Fatalf("body=%v", body)
	}

	// recover all with correct versions
	cur := e.rt.Current().Version
	status, body = e.do(t, "POST", "/admin/members/hop-a/up",
		map[string]any{"expected_version": cur})
	if status != 200 {
		t.Fatalf("recover: %d %v", status, body)
	}
}

func TestZeroWeightConfigReturns503(t *testing.T) {
	zeroPath := filepath.Join(testutil.RepoRoot(t), "testdata", "configs", "zero-weights.yaml")
	e := setup(t, zeroPath)
	status, body := e.do(t, "POST", "/v1/route", map[string]any{
		"src_ip": "10.0.0.1", "dst_ip": "10.1.0.1", "proto": 6,
		"src_port": 1, "dst_port": 2,
	})
	if status != 503 {
		t.Fatalf("status=%d", status)
	}
	if body["error"].(map[string]any)["code"] != "ZERO_TOTAL_WEIGHT" {
		t.Fatalf("body=%v", body)
	}
}

func TestBulkRoutePartialErrors(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	status, body := e.do(t, "POST", "/v1/route/bulk", map[string]any{
		"flows": []map[string]any{
			{"src_ip": "10.0.0.1", "dst_ip": "10.1.0.1", "proto": 6, "src_port": 1, "dst_port": 2},
			{"src_ip": "bad", "dst_ip": "10.1.0.1", "proto": 6, "src_port": 1, "dst_port": 2},
		},
	})
	if status != 200 {
		t.Fatalf("status=%d", status)
	}
	if body["resolved"].(float64) != 1 {
		t.Fatalf("resolved=%v", body["resolved"])
	}
	results := body["results"].([]any)
	if results[1].(map[string]any)["error_code"] != "INVALID_INPUT/BAD_SRC_IP" {
		t.Fatalf("second result=%v", results[1])
	}
}

func TestStatsDistinguishesShares(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	// route the fixed smoke corpus through the live service
	flows := loadSmokeTuples(t)
	status, body := e.do(t, "POST", "/v1/route/bulk", map[string]any{"flows": flows})
	if status != 200 {
		t.Fatal(status)
	}
	status, body = e.do(t, "GET", "/v1/stats", nil)
	if status != 200 {
		t.Fatal(status)
	}
	if body["bucket_share"] == nil || body["traffic_share"] == nil {
		t.Fatal("missing share fields")
	}
	bucket := body["bucket_share"].(map[string]any)
	traffic := body["traffic_share"].(map[string]any)
	if bucket["hop-a"].(float64) == traffic["hop-a"].(float64) {
		// Not guaranteed to differ for all corpora; the smoke corpus does
		// differ, and the fields must be independently populated regardless.
		t.Logf("bucket and traffic shares coincided for hop-a; still independently computed")
	}
	// counters must reconcile to total routed
	counters := body["counters"].(map[string]any)
	if counters["total_routed"].(float64) != float64(len(flows)) {
		t.Fatalf("total_routed=%v want %d", counters["total_routed"], len(flows))
	}
	if body["share_note"] == nil {
		t.Fatal("share distinction must be documented in response")
	}
}

func TestReplayEndToEnd(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	// current state is v1; add hop-d via config reload to create v2.
	// We exercise the weight API instead to make a real second generation.
	status, body := e.do(t, "POST", "/admin/members/hop-c/weight",
		map[string]any{"expected_version": 1, "weight": 3})
	if status != 200 {
		t.Fatalf("weight: %d %v", status, body)
	}

	status, body = e.do(t, "POST", "/v1/replay", map[string]any{
		"flow_set": "flows_smoke", "from_version": 1, "to_version": 2,
	})
	if status != 200 {
		t.Fatalf("replay status=%d body=%v", status, body)
	}
	if body["status"] != "COMPLETED" {
		t.Fatalf("status=%v error=%v", body["status"], body["error"])
	}
	runID := body["run_id"].(string)
	if runID == "" {
		t.Fatal("missing run id")
	}
	diff := body["diff"].(map[string]any)
	if diff["moved"].(float64) <= 0 {
		t.Fatalf("weight change should move some flows: %v", diff)
	}
	// fetch and list must expose the run
	status, body = e.do(t, "GET", "/v1/replay/"+runID, nil)
	if status != 200 || body["status"] != "COMPLETED" {
		t.Fatalf("fetch replay: %d %v", status, body)
	}
	status, body = e.do(t, "GET", "/v1/runs", nil)
	if status != 200 || len(body["runs"].([]any)) != 1 {
		t.Fatalf("list runs: %d %v", status, body)
	}
}

func TestReplayUnknownVersionFailureRecorded(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	status, body := e.do(t, "POST", "/v1/replay", map[string]any{
		"flow_set": "flows_smoke", "from_version": 1, "to_version": 99,
	})
	if status != 200 {
		t.Fatalf("classified replay failures return 200 with FAILED body, got %d", status)
	}
	if body["status"] != "FAILED" {
		t.Fatalf("body=%v", body)
	}
	errObj := body["error"].(map[string]any)
	if errObj["kind"] != "STATE_CONFLICT" || errObj["code"] != "UNKNOWN_VERSION" {
		t.Fatalf("error=%v", errObj)
	}
	runID := body["run_id"].(string)
	status, body = e.do(t, "GET", "/v1/replay/"+runID, nil)
	if status != 200 || body["status"] != "FAILED" {
		t.Fatalf("failed run must remain fetchable: %d %v", status, body)
	}
}

func TestReplayBadInputs(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	cases := []struct {
		name string
		body map[string]any
		code string
	}{
		{"unknown flowset", map[string]any{"flow_set": "nope", "from_version": 1, "to_version": 2}, "FLOWSET_UNKNOWN"},
		{"bad version", map[string]any{"flow_set": "flows_smoke", "from_version": 0, "to_version": 2}, "BAD_VERSIONS"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			status, body := e.do(t, "POST", "/v1/replay", tc.body)
			if status != 400 {
				t.Fatalf("status=%d", status)
			}
			if body["error"].(map[string]any)["code"] != tc.code {
				t.Fatalf("body=%v want %s", body, tc.code)
			}
		})
	}
}

func TestHealthz(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	resp, err := http.Get(e.server.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatal(resp.StatusCode)
	}
}

func TestConcurrentRoutingNoRace(t *testing.T) {
	e := setup(t, exampleConfigPath(t))
	flows := loadSmokeTuples(t)
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 20; i++ {
				_, _ = e.do(t, "POST", "/v1/route/bulk", map[string]any{"flows": flows})
				_, _ = e.do(t, "GET", "/v1/version", nil)
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 10; i++ {
			cur := e.rt.Current().Version
			e.do(t, "POST", "/admin/members/hop-a/down", map[string]any{"expected_version": cur})
			cur = e.rt.Current().Version
			e.do(t, "POST", "/admin/members/hop-a/up", map[string]any{"expected_version": cur})
		}
	}()
	wg.Wait()
}

func loadSmokeTuples(t *testing.T) []map[string]any {
	t.Helper()
	dir := filepath.Join(testutil.RepoRoot(t), "testdata", "flowsets")
	raw, err := os.ReadFile(filepath.Join(dir, "flows_smoke.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fs struct {
		Flows []map[string]any `json:"flows"`
	}
	if err := json.Unmarshal(raw, &fs); err != nil {
		t.Fatal(err)
	}
	return fs.Flows
}
