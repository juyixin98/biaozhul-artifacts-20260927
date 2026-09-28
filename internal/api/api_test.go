package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"cidrcov/internal/api"
	"cidrcov/internal/engine"
	"cidrcov/internal/store"
)

func newTestServer(t *testing.T) (*httptest.Server, *store.Store) {
	t.Helper()
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	srv := &api.Server{
		EngineOpts: engine.Options{MaxEntriesPerList: 1000},
		Store:      st,
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	return httptest.NewServer(srv.Routes()), st
}

func do(t *testing.T, method, url, body, reqID string) (int, http.Header, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != "" {
		rdr = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	if reqID != "" {
		req.Header.Set("X-Request-ID", reqID)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	return resp.StatusCode, resp.Header, m
}

func TestHealthAndVersion(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()

	if code, _, m := do(t, "GET", srv.URL+"/healthz", "", ""); code != http.StatusOK || m["status"] != "ok" {
		t.Fatalf("healthz code=%d body=%v", code, m)
	}
	code, _, m := do(t, "GET", srv.URL+"/version", "", "")
	if code != http.StatusOK || m["algorithm"] != engine.AlgorithmVersion {
		t.Fatalf("version code=%d body=%v", code, m)
	}
}

func TestCoverSuccessIsExplainable(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()

	body := `{"request_id":"case-1","allow":["10.0.0.0/24"],"exclude":["10.0.0.5/32"]}`
	code, hdr, m := do(t, "POST", srv.URL+"/v1/cover", body, "header-id-should-yield")
	if code != http.StatusOK {
		t.Fatalf("code=%d body=%v", code, m)
	}
	if hdr.Get("X-Request-ID") != "case-1" {
		t.Fatalf("body request_id should win, header=%q", hdr.Get("X-Request-ID"))
	}
	if m["request_id"] != "case-1" {
		t.Fatalf("envelope request_id=%v", m["request_id"])
	}
	versions, _ := m["versions"].(map[string]any)
	if versions["algorithm"] == nil || versions["service"] == nil {
		t.Fatalf("versions missing: %v", m["versions"])
	}
	data, _ := m["data"].(map[string]any)
	if data["status"] != "ok" {
		t.Fatalf("data status=%v", data["status"])
	}
	prefs, _ := data["prefixes"].([]any)
	if len(prefs) == 0 {
		t.Fatal("expected prefixes")
	}
	trace, _ := data["trace"].([]any)
	if len(trace) < 5 {
		t.Fatalf("trace should explain key steps, got %d", len(trace))
	}
}

func TestCoverGeneratedRequestID(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	code, hdr, _ := do(t, "POST", srv.URL+"/v1/cover", `{"allow":["0.0.0.0/0"]}`, "")
	if code != http.StatusOK {
		t.Fatalf("code=%d", code)
	}
	if !strings.HasPrefix(hdr.Get("X-Request-ID"), "req-") {
		t.Fatalf("server should generate req-* id, got %q", hdr.Get("X-Request-ID"))
	}
}

func TestCoverInvalidEntryReturns422(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	code, _, m := do(t, "POST", srv.URL+"/v1/cover", `{"allow":["bogus"]}`, "r1")
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("want 422, got %d", code)
	}
	data, _ := m["data"].(map[string]any)
	failures, _ := data["failures"].([]any)
	if len(failures) != 1 {
		t.Fatalf("want 1 failure, got %v", failures)
	}
	f0, _ := failures[0].(map[string]any)
	if f0["code"] != engine.CodeInvalidEntry || f0["list"] != "allow" {
		t.Fatalf("unexpected failure %v", f0)
	}
}

func TestCoverMalformedJSONReturns400(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	code, _, m := do(t, "POST", srv.URL+"/v1/cover", `{not json`, "r2")
	if code != http.StatusBadRequest || m["error"] == nil {
		t.Fatalf("want 400 with error, got %d %v", code, m)
	}
}

func TestCoverUnknownFieldReturns400(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	code, _, _ := do(t, "POST", srv.URL+"/v1/cover", `{"allow":[],"alow":[]}`, "r3")
	if code != http.StatusBadRequest {
		t.Fatalf("unknown field must be rejected, got %d", code)
	}
}

func TestDuplicateRequestIDReturns409(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	body := `{"request_id":"fixed","allow":["10.0.0.0/24"]}`
	if code, _, _ := do(t, "POST", srv.URL+"/v1/cover", body, ""); code != http.StatusOK {
		t.Fatalf("first write code=%d", code)
	}
	code, _, m := do(t, "POST", srv.URL+"/v1/cover", body, "")
	if code != http.StatusConflict {
		t.Fatalf("want 409, got %d", code)
	}
	if e, _ := m["error"].(map[string]any); e["code"] != "duplicate_request_id" {
		t.Fatalf("wrong error %v", m["error"])
	}
}

func TestGetAndReplay(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	body := `{"request_id":"rep-1","allow":["10.0.0.0/24","10.0.0.128/25"]}`
	if code, _, _ := do(t, "POST", srv.URL+"/v1/cover", body, ""); code != http.StatusOK {
		t.Fatal("seed failed")
	}

	code, _, m := do(t, "GET", srv.URL+"/v1/requests/rep-1", "", "")
	if code != http.StatusOK {
		t.Fatalf("get code=%d", code)
	}

	code, _, m = do(t, "POST", srv.URL+"/v1/replay/rep-1", "", "")
	if code != http.StatusOK {
		t.Fatalf("replay code=%d", code)
	}
	data, _ := m["data"].(map[string]any)
	if data["matches_recorded"] != true {
		t.Fatalf("deterministic replay must match recorded result, got %v", data["matches_recorded"])
	}
	if data["algorithm"] != engine.AlgorithmVersion {
		t.Fatalf("replay should echo algorithm version")
	}
}

func TestReplayUnknownReturns404(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	if code, _, m := do(t, "POST", srv.URL+"/v1/replay/nope", "", ""); code != http.StatusNotFound {
		t.Fatalf("want 404, got %d %v", code, m)
	}
}

func TestEmptyCoverStatus(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	body := `{"request_id":"empty-1","allow":["10.0.0.0/24"],"exclude":["10.0.0.0/24"]}`
	code, _, m := do(t, "POST", srv.URL+"/v1/cover", body, "")
	if code != http.StatusOK {
		t.Fatalf("code=%d", code)
	}
	data, _ := m["data"].(map[string]any)
	if data["status"] != "empty" {
		t.Fatalf("fully-excluded allow set must report empty, got %v", data["status"])
	}
}

func TestBodyTooLargeRejected(t *testing.T) {
	srv, _ := newTestServer(t)
	defer srv.Close()
	big := bytes.Repeat([]byte("a"), (1<<20)+10)
	req, _ := http.NewRequest("POST", srv.URL+"/v1/cover", bytes.NewReader(big))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("oversized body want 400, got %d", resp.StatusCode)
	}
}
