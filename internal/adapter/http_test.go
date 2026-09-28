package adapter

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"admission/internal/model"
)

func TestErrorBody_Contract(t *testing.T) {
	body := ErrorBody{Error: "bad", Reason: model.ReasonResourceExhausted}
	body.Category = body.Reason.Category()
	if body.Category != "resource_exhausted" {
		t.Fatalf("category=%s", body.Category)
	}
}

func TestSplitPath(t *testing.T) {
	uid, suffix := splitPath("/v1/requests/abc123/audits")
	if uid != "abc123" || suffix != "audits" {
		t.Fatalf("got %q %q", uid, suffix)
	}
	uid, suffix = splitPath("/v1/requests/abc123")
	if uid != "abc123" || suffix != "" {
		t.Fatalf("got %q %q", uid, suffix)
	}
}

func TestSplitN(t *testing.T) {
	got := splitN("Widget/shop/orders", "/", 3)
	if len(got) != 3 || got[0] != "Widget" || got[2] != "orders" {
		t.Fatalf("got %v", got)
	}
}

// A handler built without a coordinator still rejects malformed input with the
// 400 + invalid_input contract at the HTTP boundary (Decode runs before the
// coordinator is touched).
func TestPostRequest_MalformedJSON(t *testing.T) {
	h := NewHandler(nil, nil)
	req := httptest.NewRequest(http.MethodPost, "/v1/requests", strings.NewReader("{not json"))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d want 400", rec.Code)
	}
	var eb ErrorBody
	if err := json.Unmarshal(rec.Body.Bytes(), &eb); err != nil {
		t.Fatal(err)
	}
	if eb.Reason != model.ReasonInvalidInput || eb.Category != "input_error" {
		t.Fatalf("body=%+v", eb)
	}
}

func TestPostRequest_WrongMethod(t *testing.T) {
	h := NewHandler(nil, nil)
	req := httptest.NewRequest(http.MethodGet, "/v1/requests", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status=%d", rec.Code)
	}
}

func TestHealthz(t *testing.T) {
	h := NewHandler(nil, nil)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("status=%d", rec.Code)
	}
}
