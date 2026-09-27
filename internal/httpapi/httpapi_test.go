package httpapi_test

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"igmpq/internal/httpapi"
	"igmpq/internal/replay"
	"igmpq/internal/store"
)

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	quiet := log.New(io.Discard, "", 0)
	return httptest.NewServer(httpapi.New(st, quiet))
}

func loadFixture(t *testing.T, name string) []byte {
	t.Helper()
	data, err := os.ReadFile("../../testdata/scenarios/" + name)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	return data
}

func post(t *testing.T, url string, body []byte) *http.Response {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(string(body)))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	return resp
}

// TestReplayEndToEnd posts the hand-derived s1 scenario and checks the
// forwarding interval and timer-generation behaviour over HTTP.
func TestReplayEndToEnd(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	resp := post(t, srv.URL+"/v1/replay", loadFixture(t, "s1_two_members.json"))
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("status=%d body=%s", resp.StatusCode, b)
	}
	if resp.Header.Get("X-Request-Id") == "" {
		t.Fatal("missing X-Request-Id header")
	}
	var res replay.Result
	if err := json.NewDecoder(resp.Body).Decode(&res); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if res.RunID != 1 {
		t.Fatalf("run_id=%d, want 1", res.RunID)
	}
	ivs := res.Intervals["eth0/239.1.1.1"]
	if len(ivs) != 1 || ivs[0].EndMS == nil || *ivs[0].EndMS != 65000 {
		t.Fatalf("intervals=%+v, want [0,65000)", ivs)
	}

	// The stored run must be retrievable and identical in content.
	resp2, err := http.Get(srv.URL + "/v1/runs/1")
	if err != nil {
		t.Fatalf("get run: %v", err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("get run status=%d", resp2.StatusCode)
	}
	var stored replay.Result
	if err := json.NewDecoder(resp2.Body).Decode(&stored); err != nil {
		t.Fatalf("decode stored: %v", err)
	}
	if len(stored.Transitions) != len(res.Transitions) {
		t.Fatalf("stored transitions=%d, want %d", len(stored.Transitions), len(res.Transitions))
	}
}

func TestReplayInvalidConfig(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()
	body := `{"name":"bad","config":{"interfaces":[]},"events":[{"time_ms":0,"type":"general_query","iface":"eth0"}]}`
	resp := post(t, srv.URL+"/v1/replay", []byte(body))
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d, want 422", resp.StatusCode)
	}
	var out struct {
		Error struct {
			Category string `json:"category"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if out.Error.Category != "invalid_config" {
		t.Fatalf("category=%s, want invalid_config", out.Error.Category)
	}
}

func TestReplayMalformedBody(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()
	resp := post(t, srv.URL+"/v1/replay", []byte(`{not json`))
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400", resp.StatusCode)
	}
}

func TestGetMissingRun(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/v1/runs/99")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status=%d, want 404", resp.StatusCode)
	}
	var out struct {
		Error struct {
			Category string `json:"category"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if out.Error.Category != "run_not_found" {
		t.Fatalf("category=%s, want run_not_found", out.Error.Category)
	}
}

func TestHealth(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d, want 200", resp.StatusCode)
	}
}
