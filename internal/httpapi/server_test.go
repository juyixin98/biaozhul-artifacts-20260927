package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"fieldapply/internal/coord"
	"fieldapply/internal/diag"
	"fieldapply/internal/httpapi"
	"fieldapply/internal/model"
	"fieldapply/internal/store"
)

func newServer(t *testing.T) (*httptest.Server, store.Store, *coord.Coordinator) {
	t.Helper()
	st := store.NewMemory()
	co := coord.New(st, diag.NewLogger(nil))
	srv := httptest.NewServer(httpapi.New(st, co).Mux)
	t.Cleanup(func() { srv.Close(); co.Close() })
	return srv, st, co
}

func postJSON(t *testing.T, url string, body string) (int, map[string]any) {
	if t != nil {
		t.Helper()
	}
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		if t != nil {
			t.Fatal(err)
		}
		return 0, nil
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(bytes.TrimSpace(raw)) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	for i := 0; i < 2000 && !cond(); i++ {
		time.Sleep(time.Millisecond)
	}
	if !cond() {
		t.Fatal("condition never became true")
	}
}

// blockingStore blocks commits until gate closes; snapshots proceed so queued
// jobs pile up behind the single writer.
type blockingStore struct {
	store.Store
	gate      chan struct{}
	nCommit   atomic.Int64
	nSnapshot atomic.Int64
}

func (b *blockingStore) commits() int64       { return b.nCommit.Load() }
func (b *blockingStore) snapshotCalls() int64 { return b.nSnapshot.Load() }

func (b *blockingStore) Snapshot(ctx context.Context, id string) (*store.Snapshot, error) {
	b.nSnapshot.Add(1)
	return b.Store.Snapshot(ctx, id)
}

func (b *blockingStore) Commit(ctx context.Context, id string, c store.Commit) (int64, error) {
	b.nCommit.Add(1)
	select {
	case <-b.gate:
	case <-ctx.Done():
		return 0, ctx.Err()
	}
	return b.Store.Commit(ctx, id, c)
}

func TestHealthAndCreate(t *testing.T) {
	srv, _, _ := newServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("health = %d", resp.StatusCode)
	}

	status, body := postJSON(t, srv.URL+"/v1/resources", `{
		"id": "app",
		"manager": "platform",
		"body": {"spec": {"replicas": 3}},
		"schema": {}
	}`)
	if status != http.StatusCreated {
		t.Fatalf("create status = %d body=%v", status, body)
	}
	if rev := body["revision"].(float64); rev != 1 {
		t.Fatalf("revision = %v", rev)
	}
}

// End-to-end over HTTP with the real adapters: two managers, conflict, force
// takeover, delete, and an audit check via history.
func TestHTTPApplyConflictForceAndHistory(t *testing.T) {
	srv, _, _ := newServer(t)

	if status, body := postJSON(t, srv.URL+"/v1/resources", `{
		"id": "app", "manager": "a",
		"body": {"spec": {"replicas": 3, "image": "w1"}}
	}`); status != http.StatusCreated {
		t.Fatalf("create: %d %v", status, body)
	}

	apply := func(manager, body string, force bool) (int, map[string]any) {
		key := `"force":true,`
		if !force {
			key = ""
		}
		payload := `{` + key + `"manager":"` + manager + `","body":` + body + `}`
		return postJSON(t, srv.URL+"/v1/resources/app/apply", payload)
	}

	// b adopts replicas at same value (share), then a unrelated image update.
	if status, body := apply("b", `{"spec":{"replicas":3}}`, false); status != 200 {
		t.Fatalf("b share: %d %v", status, body)
	}

	// b changes replicas without force while a still shares: 409.
	status, body := apply("b", `{"spec":{"replicas":9}}`, false)
	if status != http.StatusConflict {
		t.Fatalf("want 409, got %d %v", status, body)
	}
	errObj := body["error"].(map[string]any)
	if errObj["category"] != string(model.CatStateConflict) || errObj["code"] != "field_conflict" {
		t.Fatalf("error envelope wrong: %v", errObj)
	}
	conflicts := errObj["conflicts"].([]any)
	c0 := conflicts[0].(map[string]any)
	if c0["path"] != ".spec.replicas" {
		t.Fatalf("conflict path = %v", c0["path"])
	}
	owners := c0["owners"].([]any)
	if len(owners) != 1 || owners[0] != "a" {
		t.Fatalf("conflict owners = %v", owners)
	}

	// Forced apply succeeds and takes the leaf.
	status, body = apply("b", `{"spec":{"replicas":9}}`, true)
	if status != 200 {
		t.Fatalf("forced apply: %d %v", status, body)
	}
	live := body["live"].(map[string]any)
	spec := live["spec"].(map[string]any)
	if int(spec["replicas"].(float64)) != 9 {
		t.Fatalf("replicas = %v", spec["replicas"])
	}
	// Unrelated image survived.
	if spec["image"] != "w1" {
		t.Fatalf("image lost: %v", spec["image"])
	}

	// a no longer owns replicas but still carries a stale value: its apply
	// with replicas=3 must now 409 naming b; the deletion intent for image
	// does not partially apply.
	status, body = apply("a", `{"spec":{"replicas":3}}`, false)
	if status != http.StatusConflict {
		t.Fatalf("stale owner apply: want 409, got %d %v", status, body)
	}
	c := body["error"].(map[string]any)["conflicts"].([]any)[0].(map[string]any)
	if c["path"] != ".spec.replicas" {
		t.Fatalf("conflict path = %v", c["path"])
	}
	if owners := c["owners"].([]any); len(owners) != 1 || owners[0] != "b" {
		t.Fatalf("conflict owners = %v, want [b]", owners)
	}

	// a then sends only its remaining owned field image omitted => deleted,
	// replicas (b-owned) survives untouched.
	if status, body = apply("a", `{"spec":{}}`, false); status != http.StatusOK {
		t.Fatalf("a release image: %d %v", status, body)
	}
	live = body["live"].(map[string]any)
	spec = live["spec"].(map[string]any)
	if _, has := spec["image"]; has {
		t.Fatalf("image should be released: %v", spec)
	}
	if int(spec["replicas"].(float64)) != 9 {
		t.Fatalf("b-owned replicas lost: %v", spec["replicas"])
	}

	resp, err := http.Get(srv.URL + "/v1/resources/app/history")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var hist map[string]any
	_ = json.Unmarshal(raw, &hist)
	entries := hist["history"].([]any)
	if len(entries) < 2 {
		t.Fatalf("history too short: %v", entries)
	}
}

// A stale optimistic token returns 409 revision_stale without writing.
func TestHTTPStaleRevision(t *testing.T) {
	srv, _, _ := newServer(t)
	postJSON(t, srv.URL+"/v1/resources", `{"id":"r","manager":"a","body":{"x":1}}`)

	status, body := postJSON(t, srv.URL+"/v1/resources/r/apply", `{
		"manager":"a","body":{"x":2},"baseRevision":99
	}`)
	if status != http.StatusConflict {
		t.Fatalf("status = %d, want 409 (%v)", status, body)
	}
	e := body["error"].(map[string]any)
	if e["code"] != "revision_stale" {
		t.Fatalf("code = %v", e["code"])
	}
}

func TestHTTPErrorCategories(t *testing.T) {
	srv, _, _ := newServer(t)

	// invalid_input: malformed JSON -> 400.
	resp, err := http.Post(srv.URL+"/v1/resources", "application/json", strings.NewReader(`{not json`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("malformed json status = %d, want 400", resp.StatusCode)
	}

	// invalid_input: array body -> 400.
	if status, _ := postJSON(t, srv.URL+"/v1/resources", `{"id":"r","body":[1,2]}`); status != http.StatusBadRequest {
		t.Fatalf("array body status = %d", status)
	}

	// not_found -> 404.
	if status, _ := postJSON(t, srv.URL+"/v1/resources/ghost/apply",
		`{"manager":"a","body":{}}`); status != http.StatusNotFound {
		t.Fatalf("missing resource status = %d, want 404", status)
	}

	// Create then duplicate-create -> 409 state conflict.
	postJSON(t, srv.URL+"/v1/resources", `{"id":"dup","body":{}}`)
	if status, _ := postJSON(t, srv.URL+"/v1/resources", `{"id":"dup","body":{}}`); status != http.StatusConflict {
		t.Fatalf("duplicate status = %d, want 409", status)
	}
}

// resource_exhausted: a saturated per-resource queue maps to 503.
func TestHTTPQueueFull503(t *testing.T) {
	st := store.NewMemory()
	slow := &blockingStore{Store: st, gate: make(chan struct{})}
	co := coord.New(slow, diag.NewLogger(nil))
	mux := httpapi.New(st, co).Mux
	srv := httptest.NewServer(mux)
	defer func() { srv.Close(); co.Close() }()
	ctx := context.Background()
	_, err := st.Create(ctx, "r", json.RawMessage(`{}`), json.RawMessage(`{}`), "a", model.Schema{})
	if err != nil {
		t.Fatal(err)
	}

	// Occupy the single writer + fill the queue with equal-value applies.
	got := make(chan int, 64)
	fire := func(manager, val string) {
		go func() {
			code, _ := postJSON(nil, srv.URL+"/v1/resources/r/apply",
				`{"manager":"`+manager+`","body":{"v":`+val+`}}`)
			got <- code
		}()
	}
	fire("m0", "0") // blocks in commit
	waitFor(t, func() bool { return slow.commits() >= 1 })
	for i := 0; i < 32; i++ {
		fire("mfill", "0") // occupy the buffered queue
	}
	time.Sleep(100 * time.Millisecond) // let all buffered sends land

	// With 1 in flight and 32 buffered, the queue is full: late attempts all
	// bounce immediately with 503 resource_exhausted/queue_full.
	saw503 := false
	const lateN = 12
	for i := 0; i < lateN; i++ {
		fire("late", "0")
	}
	for i := 0; i < lateN; i++ {
		select {
		case code := <-got:
			if code == http.StatusServiceUnavailable {
				saw503 = true
			} else {
				t.Fatalf("late attempt status = %d, want 503", code)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("late attempt did not bounce immediately")
		}
	}
	close(slow.gate)
	for i := 0; i < 33; i++ { // the in-flight job plus 32 buffered
		if code := <-got; code != http.StatusOK {
			t.Fatalf("drained job status = %d, want 200", code)
		}
	}
	if !saw503 {
		t.Fatal("no 503 observed under queue saturation")
	}
}
