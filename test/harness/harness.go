package harness

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"netsem/server"
)

// H is a running in-process service backed by a temporary SQLite file.
type H struct {
	TS      *httptest.Server
	BaseURL string
	cleanup func() error
}

// New starts a fresh service instance.
func New(t *testing.T) *H {
	t.Helper()
	dbPath := t.TempDir() + "/test.db"
	srv, cleanup, err := server.New(dbPath, "test-instance")
	if err != nil {
		t.Fatalf("start service: %v", err)
	}
	ts := httptest.NewServer(srv.Handler())
	return &H{TS: ts, BaseURL: ts.URL, cleanup: cleanup}
}

// Close stops the service.
func (h *H) Close(t *testing.T) {
	t.Helper()
	h.TS.Close()
	_ = h.cleanup()
}

func do(t *testing.T, method, url string, body any) (int, []byte) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("http: %v", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data
}

// PostRaw sends an already-serialized (or intentionally malformed) body.
func PostRaw(t *testing.T, url string, raw []byte) (int, []byte) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("http: %v", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data
}

// MustGet JSON-decodes a GET response and asserts the status.
func MustGet(t *testing.T, url string, wantStatus int, v any) {
	t.Helper()
	code, data := do(t, http.MethodGet, url, nil)
	if code != wantStatus {
		t.Fatalf("GET %s: status %d want %d: %s", url, code, wantStatus, data)
	}
	if v != nil {
		if err := json.Unmarshal(data, v); err != nil {
			t.Fatalf("decode %s: %v: %s", url, err, data)
		}
	}
}
