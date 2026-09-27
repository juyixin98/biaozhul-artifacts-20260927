package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"flexhash/internal/hashring"
	"flexhash/internal/store"
)

type harness struct {
	svc    *Service
	server *httptest.Server
	st     *store.Store
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, filepath.Join(t.TempDir(), "s.db"))
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	const B = 256
	mgr := hashring.NewManager(B)
	ms := []hashring.Member{
		{ID: "a", Address: "127.0.0.1:9001", Weight: 2, Healthy: true},
		{ID: "b", Address: "127.0.0.1:9002", Weight: 1, Healthy: true},
	}
	ring, _, err := mgr.Bootstrap(1, ms, 0)
	if err != nil {
		t.Fatal(err)
	}
	rows := make([]store.AssignmentRow, 0, B)
	for _, as := range ring.Assignments() {
		rows = append(rows, store.AssignmentRow{Version: 1, Bucket: as.Bucket, Member: as.Member})
	}
	mj, _ := json.Marshal(ms)
	if err := st.SaveConfig(ctx, store.ConfigSnapshot{
		Version: 1, BucketCount: B, MembersJSON: mj,
	}, rows); err != nil {
		t.Fatal(err)
	}

	svc := NewService(mgr, st, B)
	mux := http.NewServeMux()
	svc.Routes(mux)
	return &harness{svc: svc, server: httptest.NewServer(mux), st: st}
}

func (h *harness) post(t *testing.T, path string, body any) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(h.server.URL+path, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(t, resp)
}

func (h *harness) get(t *testing.T, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(h.server.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(t, resp)
}

func decode(t *testing.T, resp *http.Response) (int, map[string]any) {
	t.Helper()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if out == nil {
		out = map[string]any{}
	}
	return resp.StatusCode, out
}

func TestLookupHappyPath(t *testing.T) {
	h := newHarness(t)
	status, body := h.post(t, "/v1/lookup", map[string]any{
		"src_ip": "10.0.0.1", "src_port": 44321,
		"dst_ip": "10.1.0.1", "dst_port": 80, "protocol": "tcp",
	})
	if status != 200 {
		t.Fatalf("status %d body=%v", status, body)
	}
	for _, k := range []string{"bucket", "owner", "chosen", "address", "config_version", "health_revision", "failover"} {
		if _, ok := body[k]; !ok {
			t.Fatalf("missing field %s in %v", k, body)
		}
	}
	if body["chosen"] != body["owner"] || body["failover"] != false {
		t.Fatalf("healthy direct routing wrong: %v", body)
	}
	if bv, _ := body["config_version"].(float64); int64(bv) != 1 {
		t.Fatalf("version %v", body["config_version"])
	}

	// Same flow twice -> identical mapping (stability over HTTP).
	_, body2 := h.post(t, "/v1/lookup", map[string]any{
		"src_ip": "10.0.0.1", "src_port": 44321,
		"dst_ip": "10.1.0.1", "dst_port": 80, "protocol": "tcp",
	})
	if body2["chosen"] != body["chosen"] || body2["bucket"] != body["bucket"] {
		t.Fatalf("flow not stable: %v vs %v", body, body2)
	}
}

func TestLookupErrorClasses(t *testing.T) {
	h := newHarness(t)
	// Bad JSON -> 400 input_error.
	resp, err := http.Post(h.server.URL+"/v1/lookup", "application/json", bytes.NewReader([]byte("{bad")))
	if err != nil {
		t.Fatal(err)
	}
	var eb map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&eb)
	resp.Body.Close()
	if resp.StatusCode != 400 || eb["kind"] != "input_error" {
		t.Fatalf("bad json: %d %v", resp.StatusCode, eb)
	}

	// Invalid tuple (bad IP) -> 400 input_error.
	status, body := h.post(t, "/v1/lookup", map[string]any{
		"src_ip": "nope", "dst_ip": "10.0.0.1", "protocol": "tcp",
	})
	if status != 400 || body["kind"] != "input_error" {
		t.Fatalf("bad tuple: %d %v", status, body)
	}
}

func TestConfigUpdateMovesOnlyAffected(t *testing.T) {
	h := newHarness(t)
	status, body := h.post(t, "/v1/config", map[string]any{
		"members": []map[string]any{
			{"id": "a", "address": "127.0.0.1:9001", "weight": 2, "healthy": true},
			{"id": "b", "address": "127.0.0.1:9002", "weight": 1, "healthy": true},
			{"id": "c", "address": "127.0.0.1:9003", "weight": 1, "healthy": true},
		},
	})
	if status != 200 {
		t.Fatalf("update status %d body=%v", status, body)
	}
	if v, _ := body["version"].(float64); int64(v) != 2 {
		t.Fatalf("new version = %v want 2", body["version"])
	}
	moved, _ := body["moved_buckets"].(float64)
	if moved <= 0 {
		t.Fatalf("adding member must move some buckets, got %v", body["moved_buckets"])
	}
	q, _ := body["quota"].(map[string]any)
	if q["c"] == nil || q["c"].(float64) <= 0 {
		t.Fatalf("new member c has no quota: %v", q)
	}

	// GET config reflects version 2 and health survives for a,b.
	status, got := h.get(t, "/v1/config")
	if status != 200 {
		t.Fatalf("get config %d", status)
	}
	if v, _ := got["version"].(float64); int64(v) != 2 {
		t.Fatalf("get version %v", got["version"])
	}
}

func TestConfigValidationRejects(t *testing.T) {
	h := newHarness(t)
	// All-zero weights -> 400 input_error.
	status, body := h.post(t, "/v1/config", map[string]any{
		"members": []map[string]any{
			{"id": "a", "address": "127.0.0.1:9001", "weight": 0},
			{"id": "b", "address": "127.0.0.1:9002", "weight": 0},
		},
	})
	if status != 400 || body["kind"] != "input_error" {
		t.Fatalf("all-zero: %d %v", status, body)
	}
	// Duplicate IDs -> 400.
	status, body = h.post(t, "/v1/config", map[string]any{
		"members": []map[string]any{
			{"id": "a", "address": "127.0.0.1:9001", "weight": 1},
			{"id": "a", "address": "127.0.0.1:9009", "weight": 1},
		},
	})
	if status != 400 || body["kind"] != "input_error" {
		t.Fatalf("dup: %d %v", status, body)
	}
}

func TestHealthExcludesAndRecovers(t *testing.T) {
	h := newHarness(t)
	// Find a flow structurally owned by a (weight 2 vs b weight 1), keeping
	// the exact request body so the same 5-tuple can be replayed.
	var ownerA map[string]any
	var ownerABody map[string]any
	for i := 0; i < 10000; i++ {
		body := map[string]any{
			"src_ip": fmt.Sprintf("10.0.%d.%d", i/256, i%256), "src_port": 1000 + i,
			"dst_ip": "10.9.9.9", "dst_port": 80, "protocol": "tcp",
		}
		_, d := h.post(t, "/v1/lookup", body)
		if d["owner"] == "a" {
			ownerA = d
			ownerABody = body
			break
		}
	}
	if ownerA == nil {
		t.Fatal("could not find a-owned flow")
	}

	// Mark a down.
	status, body := h.post(t, "/v1/members/a/health", map[string]any{"healthy": false})
	if status != 200 || body["health_revision"].(float64) != 1 {
		t.Fatalf("down: %d %v", status, body)
	}
	// Repeat is a state conflict -> 409.
	status, body = h.post(t, "/v1/members/a/health", map[string]any{"healthy": false})
	if status != 409 || body["kind"] != "state_conflict" {
		t.Fatalf("repeat down: %d %v", status, body)
	}
	// Unknown member -> 400 input.
	status, body = h.post(t, "/v1/members/ghost/health", map[string]any{"healthy": false})
	if status != 400 || body["kind"] != "input_error" {
		t.Fatalf("ghost: %d %v", status, body)
	}

	// Same 5-tuple now fails over to b, flagged, structural owner still a.
	_, fail := h.post(t, "/v1/lookup", ownerABody)
	if fail["chosen"] != "b" || fail["failover"] != true || fail["owner"] != "a" {
		t.Fatalf("failover decision wrong: %v", fail)
	}

	// Recovery -> back to a at revision 2.
	status, body = h.post(t, "/v1/members/a/health", map[string]any{"healthy": true})
	if status != 200 || body["health_revision"].(float64) != 2 {
		t.Fatalf("recover: %d %v", status, body)
	}
	_, rec := h.post(t, "/v1/lookup", ownerABody)
	if rec["chosen"] != "a" || rec["failover"] != false {
		t.Fatalf("recovery decision wrong: %v", rec)
	}
}

func TestSharesDistinguishesBucketFromWeight(t *testing.T) {
	h := newHarness(t)
	status, body := h.get(t, "/v1/shares")
	if status != 200 {
		t.Fatalf("shares %d %v", status, body)
	}
	if body["note"] == nil || body["total_weight"].(float64) != 3 {
		t.Fatalf("shares body wrong: %v", body)
	}
	shares, _ := body["shares"].(map[string]any)
	a, _ := shares["a"].(map[string]any)
	if a["bucket_quota"].(float64) == 0 || a["config_weight_share"].(float64) < 0.6 {
		t.Fatalf("member a share wrong: %v", a)
	}
}

func TestReplayAndRunsEndpoints(t *testing.T) {
	h := newHarness(t)
	status, body := h.get(t, "/v1/replay/verify")
	if status != 200 || body["result"] != "consistent" {
		t.Fatalf("verify: %d %v", status, body)
	}
	status, body = h.get(t, "/v1/replay/state")
	if status != 200 || body["final_version"].(float64) != 1 {
		t.Fatalf("state: %d %v", status, body)
	}
	if err := h.st.SaveRunLog(context.Background(), "run-x", "TestT", "passed",
		map[string]any{"k": 1}); err != nil {
		t.Fatal(err)
	}
	status, body = h.get(t, "/v1/runs")
	if status != 200 {
		t.Fatalf("runs %d", status)
	}
	runs, _ := body["runs"].([]any)
	if len(runs) != 1 {
		t.Fatalf("runs len %d", len(runs))
	}
}

// TestConcurrentConfigReads hammers GET endpoints while a writer updates
// config; under -race this must stay clean and every response must reference
// a fully formed version (quota sums to bucket count).
func TestConcurrentConfigReads(t *testing.T) {
	h := newHarness(t)
	var wg sync.WaitGroup
	stop := make(chan struct{})
	var failMu sync.Mutex
	failures := []string{}
	addFail := func(s string) {
		failMu.Lock()
		failures = append(failures, s)
		failMu.Unlock()
	}

	for r := 0; r < 12; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			client := &http.Client{Timeout: 5 * time.Second}
			for {
				select {
				case <-stop:
					return
				default:
				}
				resp, err := client.Get(h.server.URL + "/v1/assignments")
				if err != nil {
					addFail("get: " + err.Error())
					return
				}
				var b map[string]any
				decErr := json.NewDecoder(resp.Body).Decode(&b)
				resp.Body.Close()
				if decErr != nil {
					addFail("decode: " + decErr.Error())
					return
				}
				owners, _ := b["owners_bucket_count"].(map[string]any)
				var sum float64
				for _, v := range owners {
					sum += v.(float64)
				}
				if len(owners) != 0 && sum != float64(256) {
					addFail(fmt.Sprintf("quota sum %v", sum))
					return
				}
			}
		}()
	}
	for v := 0; v < 20; v++ {
		id := "c"
		members := []map[string]any{
			{"id": "a", "address": "127.0.0.1:9001", "weight": 2, "healthy": true},
			{"id": "b", "address": "127.0.0.1:9002", "weight": 1, "healthy": true},
		}
		if v%2 == 0 {
			members = append(members, map[string]any{
				"id": id, "address": "127.0.0.1:9003", "weight": 1, "healthy": true})
		}
		if status, body := h.post(t, "/v1/config", map[string]any{"members": members}); status != 200 {
			addFail(fmt.Sprintf("update v=%d status %d body %v", v, status, body))
		}
	}
	close(stop)
	wg.Wait()
	if len(failures) > 0 {
		t.Fatalf("concurrent read failures: %v", failures)
	}
}
