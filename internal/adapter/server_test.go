package adapter

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"netpolicy/internal/diag"
	"netpolicy/internal/engine"
	"netpolicy/internal/reconcile"
	"netpolicy/internal/source"
	"netpolicy/internal/store"
)

func newTestServer(t *testing.T, fixture string) (*Server, *store.Store, *reconcile.Loop) {
	t.Helper()
	st, err := store.Open(context.Background(), "file:"+filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	raw, err := filepath.Abs(filepath.Join("..", "..", "test", "fixtures", "scenarios", fixture))
	if err != nil {
		t.Fatal(err)
	}
	snap, err := (&source.FixtureSource{Path: raw}).Fetch(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := st.SaveSnapshot(context.Background(), snap); err != nil {
		t.Fatal(err)
	}
	holder := &Holder{}
	holder.Set(engine.New(snap))
	rec := reconcile.New(&source.FixtureSource{Path: raw}, st)
	loop := reconcile.NewLoop(rec, 0, diag.NewLogger("debug", &bytes.Buffer{}))
	srv := &Server{Holder: holder, Store: st, Loop: loop, Logger: diag.NewLogger("debug", &bytes.Buffer{})}
	return srv, st, loop
}

func doJSON(t *testing.T, h http.Handler, method, path string, body any, rid string) (int, map[string]any) {
	t.Helper()
	var r *http.Request
	if body != nil {
		b, _ := json.Marshal(body)
		r = httptest.NewRequest(method, path, bytes.NewReader(b))
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	r.Header.Set("Content-Type", "application/json")
	if rid != "" {
		r.Header.Set(diag.RequestIDHeader, rid)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	var out map[string]any
	if w.Body.Len() > 0 {
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatalf("non-json response %d: %s", w.Code, w.Body.String())
		}
	}
	return w.Code, out
}

func TestCheckEndpointAllowAndDeny(t *testing.T) {
	srv, _, _ := newTestServer(t, "overlapping-selectors.json")
	h := srv.NewRouter()

	code, body := doJSON(t, h, "POST", "/v1/check", map[string]any{
		"sourceUid": "u-api", "destUid": "u-web-a", "protocol": "TCP", "port": 8080,
	}, "")
	if code != http.StatusOK {
		t.Fatalf("status %d body %v", code, body)
	}
	if body["verdict"] != "ALLOW" || body["allowed"] != true {
		t.Fatalf("expected ALLOW, got %v", body)
	}
	ing, _ := body["ingress"].(map[string]any)
	if ing["isolated"] != true {
		t.Fatalf("ingress side should be isolated: %v", ing)
	}

	// Denied reverse path: devbox -> web is rejected on the ingress side.
	code, body = doJSON(t, h, "POST", "/v1/check", map[string]any{
		"sourceUid": "u-devbox", "destUid": "u-web-a", "protocol": "TCP", "port": 8080,
	}, "")
	if body["verdict"] != "DENY" || body["reason"] != "ingress_selected_no_rule_matched" {
		t.Fatalf("expected ingress default deny, got %v", body)
	}
}

func TestCheckBadRequestCategories(t *testing.T) {
	srv, _, _ := newTestServer(t, "overlapping-selectors.json")
	h := srv.NewRouter()

	code, body := doJSON(t, h, "POST", "/v1/check", map[string]any{
		"sourceUid": "u-api", "destUid": "u-web-a", "protocol": "SCTP", "port": 8080,
	}, "")
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
	if errBody, _ := body["error"].(map[string]any); errBody["code"] != "invalid_protocol" {
		t.Fatalf("error code = %v", body["error"])
	}

	// Unknown endpoint is structurally callable -> 200 with UNDECIDABLE.
	code, body = doJSON(t, h, "POST", "/v1/check", map[string]any{
		"sourceUid": "ghost", "destUid": "u-web-a", "protocol": "TCP", "port": 8080,
	}, "")
	if code != http.StatusOK || body["verdict"] != "UNDECIDABLE" || body["reason"] != "endpoint_unknown" {
		t.Fatalf("unknown endpoint: code=%d body=%v", code, body)
	}

	// Malformed JSON -> 400 malformed_json.
	r := httptest.NewRequest("POST", "/v1/check", strings.NewReader("{not json"))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "malformed_json") {
		t.Fatalf("malformed body: %d %s", w.Code, w.Body.String())
	}
}

func TestRequestIDIsHonoredAndEchoed(t *testing.T) {
	srv, _, _ := newTestServer(t, "overlapping-selectors.json")
	h := srv.NewRouter()
	r := httptest.NewRequest("POST", "/v1/check", strings.NewReader(`{"sourceUid":"u-api","destUid":"u-web-a","protocol":"TCP","port":8080}`))
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set(diag.RequestIDHeader, "corr-1234")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if got := w.Header().Get(diag.RequestIDHeader); got != "corr-1234" {
		t.Fatalf("echoed request id = %q, want corr-1234", got)
	}
	// Success responses carry the correlation id in the response header;
	// the body is the domain decision itself.
	if strings.Contains(w.Body.String(), "corr-1234") {
		t.Fatalf("decision body should not embed request id: %s", w.Body.String())
	}
}

func TestMatrixEndpoint(t *testing.T) {
	srv, _, _ := newTestServer(t, "overlapping-selectors.json")
	h := srv.NewRouter()
	code, body := doJSON(t, h, "POST", "/v1/matrix", map[string]any{
		"protocol": "TCP", "port": 8080,
	}, "")
	if code != http.StatusOK {
		t.Fatalf("status %d %v", code, body)
	}
	cells, _ := body["cells"].([]any)
	if len(cells) != 36 { // 6x6
		t.Fatalf("matrix cells = %d, want 36", len(cells))
	}

	code, body = doJSON(t, h, "POST", "/v1/matrix", map[string]any{
		"allDeclaredPorts": true,
	}, "")
	if code != http.StatusOK {
		t.Fatalf("allDeclaredPorts status %d %v", code, body)
	}
	mats, _ := body["matrices"].([]any)
	if len(mats) != 3 { // 8080/tcp, 9090/tcp, 5432/tcp
		t.Fatalf("declared-port sweeps = %d, want 3", len(mats))
	}
}

func TestStatusAndSnapshots(t *testing.T) {
	srv, _, _ := newTestServer(t, "overlapping-selectors.json")
	h := srv.NewRouter()
	code, body := doJSON(t, h, "GET", "/v1/status", nil, "")
	if code != http.StatusOK || body["revision"] == nil {
		t.Fatalf("status: %d %v", code, body)
	}
	code, body = doJSON(t, h, "GET", "/v1/snapshots/1", nil, "")
	if code != http.StatusOK {
		t.Fatalf("get snapshot: %d", code)
	}
	if body["revision"].(float64) != 1 {
		t.Fatalf("snapshot revision = %v", body["revision"])
	}
	code, body = doJSON(t, h, "GET", "/v1/snapshots/99", nil, "")
	if code != http.StatusNotFound {
		t.Fatalf("missing snapshot code = %d", code)
	}
	if errBody, _ := body["error"].(map[string]any); errBody["code"] != "snapshot_not_found" {
		t.Fatalf("error = %v", body["error"])
	}
}

func TestPinRevisionConflict(t *testing.T) {
	srv, _, _ := newTestServer(t, "overlapping-selectors.json")
	h := srv.NewRouter()
	code, body := doJSON(t, h, "POST", "/v1/check", map[string]any{
		"sourceUid": "u-api", "destUid": "u-web-a", "protocol": "TCP", "port": 8080,
		"pinRevision": 4242,
	}, "")
	if code != http.StatusOK || body["verdict"] != "UNDECIDABLE" || body["reason"] != "revision_conflict" {
		t.Fatalf("pin mismatch: %d %v", code, body)
	}
}
