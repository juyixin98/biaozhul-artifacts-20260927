package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"pathvector/internal/replay"
	"pathvector/internal/store"
)

func newTestServer(t *testing.T) (*Server, http.Handler) {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	r := replay.NewRunner(st)
	s := &Server{Runner: r, Store: st, FixtureDir: filepath.Join("..", "..", "testdata", "fixtures"), MaxBodySize: 1 << 20}
	return s, s.NewRouter()
}

func do(t *testing.T, h http.Handler, method, path string, body any) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		switch v := body.(type) {
		case string:
			rdr = bytes.NewReader([]byte(v))
		case []byte:
			rdr = bytes.NewReader(v)
		default:
			b, _ := json.Marshal(v)
			rdr = bytes.NewReader(b)
		}
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var parsed map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &parsed)
	}
	return rec, parsed
}

// TestReplayFixtureHappyPath exercises the full HTTP -> engine -> store ->
// read-back loop with a converged fixture.
func TestReplayFixtureHappyPath(t *testing.T) {
	_, h := newTestServer(t)
	rec, body := do(t, h, "POST", "/v1/replays/fixtures", map[string]string{"name": "multi_exit"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	runID, _ := body["run_id"].(string)
	if runID == "" {
		t.Fatal("missing run_id")
	}
	report := body["report"].(map[string]any)
	if report["status"] != "converged" {
		t.Fatalf("status=%v", report["status"])
	}

	// Read back through GET endpoints.
	rec2, getBody := do(t, h, "GET", "/v1/runs/"+runID, nil)
	if rec2.Code != http.StatusOK {
		t.Fatalf("get run status=%d", rec2.Code)
	}
	if getBody["report"] == nil {
		t.Fatal("stored report missing")
	}
	rec3, traceBody := do(t, h, "GET", "/v1/runs/"+runID+"/trace", nil)
	if rec3.Code != http.StatusOK {
		t.Fatalf("get trace status=%d", rec3.Code)
	}
	trace, _ := traceBody["trace"].([]any)
	if len(trace) == 0 {
		t.Fatal("stored trace empty")
	}
	rec4, listBody := do(t, h, "GET", "/v1/runs", nil)
	if rec4.Code != http.StatusOK || len(listBody["runs"].([]any)) != 1 {
		t.Fatalf("list: code=%d body=%v", rec4.Code, listBody)
	}
}

// TestErrorStatusMapping checks each error category maps to the required
// HTTP status and keeps the structured kind in the body.
func TestErrorStatusMapping(t *testing.T) {
	_, h := newTestServer(t)

	// 400 invalid_input (malformed JSON).
	rec, body := do(t, h, "POST", "/v1/replays", "{not json")
	if rec.Code != http.StatusBadRequest || body["kind"] != "invalid_input" {
		t.Fatalf("malformed: code=%d kind=%v", rec.Code, body["kind"])
	}

	// 400 invalid fixture name.
	rec, body = do(t, h, "POST", "/v1/replays/fixtures", map[string]string{"name": "../escape"})
	if rec.Code != http.StatusBadRequest || body["kind"] != "invalid_input" {
		t.Fatalf("bad name: code=%d kind=%v", rec.Code, body["kind"])
	}

	// 409 state_conflict with run_id for trace retrieval.
	rec, body = do(t, h, "POST", "/v1/replays/fixtures", map[string]string{"name": "unknown_withdraw"})
	if rec.Code != http.StatusConflict || body["kind"] != "state_conflict" {
		t.Fatalf("state conflict: code=%d body=%s", rec.Code, rec.Body.String())
	}
	runID, _ := body["run_id"].(string)
	if runID == "" {
		t.Fatal("409 must still carry run_id")
	}

	// 429 resource_exhausted.
	rec, body = do(t, h, "POST", "/v1/replays/fixtures", map[string]string{"name": "queue_cap"})
	if rec.Code != http.StatusTooManyRequests || body["kind"] != "resource_exhausted" {
		t.Fatalf("queue cap: code=%d kind=%v", rec.Code, body["kind"])
	}

	// 404 unknown run.
	rec, body = do(t, h, "GET", "/v1/runs/nope", nil)
	if rec.Code != http.StatusNotFound || body["kind"] != "not_found" {
		t.Fatalf("missing run: code=%d kind=%v", rec.Code, body["kind"])
	}
}

// TestNonConvergedIsHTTP200: budget and oscillation are verdicts, not
// errors, so replay endpoints answer 200 with status not_converged.
func TestNonConvergedIsHTTP200(t *testing.T) {
	_, h := newTestServer(t)
	for _, name := range []string{"budget", "oscillation"} {
		rec, body := do(t, h, "POST", "/v1/replays/fixtures", map[string]string{"name": name})
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: code=%d body=%s", name, rec.Code, rec.Body.String())
		}
		report := body["report"].(map[string]any)
		if report["status"] != "not_converged" {
			t.Fatalf("%s status=%v", name, report["status"])
		}
		if name == "oscillation" && report["cycle"] == nil {
			t.Fatal("oscillation verdict lacks cycle evidence")
		}
	}
}

func TestBodyTooLargeClassified(t *testing.T) {
	s, _ := newTestServer(t)
	s.MaxBodySize = 32 // must be set before the router wraps the handlers
	h := s.NewRouter()
	rec, body := do(t, h, "POST", "/v1/replays", strings.Repeat("x", 4096))
	if rec.Code != http.StatusTooManyRequests || body["kind"] != "resource_exhausted" {
		t.Fatalf("oversize: code=%d kind=%v body=%s", rec.Code, body["kind"], rec.Body.String())
	}
}

func TestHealthz(t *testing.T) {
	_, h := newTestServer(t)
	rec, body := do(t, h, "GET", "/healthz", nil)
	if rec.Code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("healthz code=%d body=%v", rec.Code, body)
	}
}
