package httpapi_test

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

	"github.com/local/evictioncoordinator/internal/coordinator"
	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/fixture"
	"github.com/local/evictioncoordinator/internal/httpapi"
	"github.com/local/evictioncoordinator/internal/store"
)

type env struct {
	srv   *httptest.Server
	st    *store.Store
	coord *coordinator.Coordinator
	ad    *fixture.Adapter
}

func setup(t *testing.T) *env {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, filepath.Join(t.TempDir(), "http.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	ad := fixture.NewAdapter(st, time.Now)
	coord := coordinator.New(st, coordinator.Config{ApprovalTTL: time.Minute})
	h := httpapi.NewServer(httpapi.Deps{
		Store:           st,
		Coordinator:     coord,
		EmitObservation: ad.EmitObservation,
		EmitFailure:     ad.EmitFailure,
	})
	srv := httptest.NewServer(h.Handler)
	t.Cleanup(srv.Close)
	return &env{srv: srv, st: st, coord: coord, ad: ad}
}

func postJSON(t *testing.T, url string, body any, requestID string) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	if requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func get(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func createGroup(t *testing.T, e *env) {
	t.Helper()
	st, body := postJSON(t, e.srv.URL+"/api/v1/groups", map[string]any{
		"name": "web", "namespace": "default", "replicas": 3,
		"budget":   map[string]any{"mode": "maxUnavailable", "value": 1},
		"selector": map[string]string{"app": "web"},
	}, "req-create")
	if st != http.StatusCreated {
		t.Fatalf("create group status=%d body=%v", st, body)
	}
	if body["request_id"] != "req-create" {
		t.Fatalf("request_id not echoed: %v", body["request_id"])
	}
}

func observe(t *testing.T, e *env, id string, ready bool, epoch int64) {
	t.Helper()
	st, body := postJSON(t, e.srv.URL+"/api/v1/observations", map[string]any{
		"instance": map[string]any{
			"id": id, "namespace": "default", "group": "web",
			"labels": map[string]string{"app": "web"},
		},
		"ready": ready, "epoch": epoch,
	}, "")
	if st != http.StatusAccepted {
		t.Fatalf("observe %s status=%d body=%v", id, st, body)
	}
}

func evictURL(e *env) string {
	return e.srv.URL + "/api/v1/namespaces/default/groups/web/eviction"
}

func TestHTTP_FullHappyPathAndExhaustion(t *testing.T) {
	e := setup(t)
	createGroup(t, e)
	observe(t, e, "a", true, 1)
	observe(t, e, "b", true, 1)
	observe(t, e, "c", true, 1)

	st, first := postJSON(t, evictURL(e), map[string]any{"instance_id": "a"}, "req-evict-a")
	if st != http.StatusAccepted {
		t.Fatalf("first evict status=%d body=%v", st, first)
	}
	if first["accepted"] != true || first["approval_id"] == nil {
		t.Fatalf("first evict body=%v", first)
	}
	snap := first["snapshot"].(map[string]any)
	if snap["available_slots"].(float64) != 0 {
		t.Fatalf("post-accept slots=%v want 0", snap["available_slots"])
	}

	st, second := postJSON(t, evictURL(e), map[string]any{"instance_id": "b"}, "req-evict-b")
	if st != http.StatusUnprocessableEntity {
		t.Fatalf("second evict status=%d want 422, body=%v", st, second)
	}
	if second["category"] != string(domain.CatBudgetExhausted) {
		t.Fatalf("category=%v want budget_exhausted", second["category"])
	}
	if second["request_id"] != "req-evict-b" {
		t.Fatal("request id not carried through to rejection")
	}
}

func TestHTTP_CannotDecideIs409Not422(t *testing.T) {
	e := setup(t)
	createGroup(t, e)
	observe(t, e, "a", true, 1)
	observe(t, e, "b", true, 1)
	// member c has NO observation -> group not fresh.
	st, body := postJSON(t, evictURL(e), map[string]any{"instance_id": "a"}, "")
	if st != http.StatusConflict {
		t.Fatalf("status=%d want 409 (cannot decide), body=%v", st, body)
	}
	if body["category"] != string(domain.CatUnknownReadiness) {
		t.Fatalf("category=%v want unknown_readiness", body["category"])
	}
}

func TestHTTP_InvoluntaryFailureIsDistinctCategory(t *testing.T) {
	e := setup(t)
	createGroup(t, e)
	observe(t, e, "a", true, 1)
	observe(t, e, "b", true, 1)
	observe(t, e, "c", true, 1)

	st, fbody := postJSON(t, e.srv.URL+"/api/v1/failures", map[string]any{
		"instance_id": "a", "reason": "node-lost", "epoch": 1,
	}, "")
	if st != http.StatusAccepted {
		t.Fatalf("record failure status=%d body=%v", st, fbody)
	}

	st, body := postJSON(t, evictURL(e), map[string]any{"instance_id": "a"}, "")
	if st != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d want 422", st)
	}
	if body["category"] != string(domain.CatInstanceFailed) {
		t.Fatalf("category=%v want instance_failed, never budget_exhausted", body["category"])
	}
}

// TestHTTP_ConcurrentRequestsOverTheWire is an independent end-to-end check of
// atomic reservation through the real transport: 6 parallel POSTs, budget 1.
func TestHTTP_ConcurrentRequestsOverTheWire(t *testing.T) {
	e := setup(t)
	createGroup(t, e)
	for _, id := range []string{"a", "b", "c", "d", "e", "f"} {
		observe(t, e, id, true, 1)
	}

	const n = 6
	var wg sync.WaitGroup
	statuses := make([]int, n)
	accepted := make([]bool, n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			id := string(rune('a' + i))
			st, body := postJSON(t, evictURL(e), map[string]any{"instance_id": id},
				fmt.Sprintf("req-%d", i))
			statuses[i] = st
			accepted[i] = body["accepted"] == true
		}(i)
	}
	close(start)
	wg.Wait()

	accepts := 0
	for i := range accepted {
		if accepted[i] {
			accepts++
		}
	}
	if accepts != 1 {
		t.Fatalf("accepted over HTTP=%d want exactly 1; statuses=%v", accepts, statuses)
	}
}

// TestHTTP_StaleClientEpochRejected ensures a client acting on an old selector
// version gets the specific stale category.
func TestHTTP_StaleClientEpochRejected(t *testing.T) {
	e := setup(t)
	createGroup(t, e)
	for _, id := range []string{"a", "b", "c"} {
		observe(t, e, id, true, 1)
	}
	// bump the selector epoch to 2
	st, body := postJSON(t, e.srv.URL+"/api/v1/groups/default/web/selectors:bump",
		map[string]any{}, "")
	if st != http.StatusOK || body["new_selector_epoch"].(float64) != 2 {
		t.Fatalf("bump status=%d body=%v", st, body)
	}
	// client still believes epoch 1
	st, dec := postJSON(t, evictURL(e),
		map[string]any{"instance_id": "b", "client_selector_epoch": 1}, "")
	if st != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d want 422", st)
	}
	if dec["category"] != string(domain.CatStaleEpoch) {
		t.Fatalf("category=%v want stale_selector_epoch", dec["category"])
	}
}
