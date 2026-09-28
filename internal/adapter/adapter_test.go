package adapter_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"replicactl/internal/adapter"
	"replicactl/internal/config"
	"replicactl/internal/controller"
	"replicactl/internal/model"
	"replicactl/internal/store"
)

type fixedClock struct{ t time.Time }

func (f *fixedClock) Now() time.Time { return f.t }

func setup(t *testing.T, initial int32) (*httptest.Server, *store.Store, *fixedClock) {
	t.Helper()
	clk := &fixedClock{t: time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)}
	st, err := store.New(context.Background(), "file:"+filepath.Join(t.TempDir(), "http.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })

	c := config.Default()
	c.InitialReplicas = initial
	if _, err := st.SaveConfig(context.Background(), c, clk.Now().Format(time.RFC3339Nano)); err != nil {
		t.Fatal(err)
	}
	if err := st.SeedFleet(context.Background(), initial, clk.Now()); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	eng := controller.NewEngine(st, clk)
	srv := adapter.NewServer(eng, st, log, clk.Now)
	return httptest.NewServer(srv.Handler()), st, clk
}

func do(t *testing.T, method, url, body string, rid *string) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != "" {
		rdr = bytes.NewBufferString(body)
	}
	req, _ := http.NewRequest(method, url, rdr)
	if rid != nil {
		req.Header.Set("X-Request-ID", *rid)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	echo := resp.Header.Get("X-Request-ID")
	if rid != nil && echo != *rid {
		t.Fatalf("request id not correlated: sent %s got %s", *rid, echo)
	}
	return resp.StatusCode, m
}

// End to end: ingest three high, fresh samples then reconcile and read back
// the explainable decision by its request id.
func TestHTTPIngestReconcileAndFetch(t *testing.T) {
	srv, _, clk := setup(t, 3)
	defer srv.Close()

	for _, id := range []string{"ins-0001", "ins-0002", "ins-0003"} {
		status, body := do(t, "POST", srv.URL+"/v1/instances/"+id+"/samples",
			`{"value":200}`, nil)
		if status != http.StatusAccepted {
			t.Fatalf("ingest %s status=%d body=%v", id, status, body)
		}
	}
	rid := "req-http-stepup"
	status, body := do(t, "POST", srv.URL+"/v1/reconcile", "", &rid)
	if status != http.StatusOK {
		t.Fatalf("reconcile status=%d body=%v", status, body)
	}
	if body["request_id"] != rid {
		t.Fatalf("decision request_id = %v, want %s", body["request_id"], rid)
	}
	if body["action"] != "scale_up" {
		t.Fatalf("want scale_up, got %v", body["action"])
	}
	if act(body) != float64(6) {
		t.Fatalf("want applied 6, got %v", act(body))
	}
	if body["location"] != "internal/controller.Reconcile" {
		t.Fatalf("decision must name processing location, got %v", body["location"])
	}
	if body["config_version"] == nil || body["config_revision"] == nil {
		t.Fatalf("decision must carry config version/revision: %v", body)
	}

	// Read back through the correlation id endpoint.
	status2, body2 := do(t, "GET", srv.URL+"/v1/decisions/"+rid, "", nil)
	if status2 != http.StatusOK || body2["request_id"] != rid {
		t.Fatalf("fetch by request id failed: %d %v", status2, body2)
	}

	// Fleet reflects the new size with non-reused ids.
	status3, fleet := do(t, "GET", srv.URL+"/v1/fleet", "", nil)
	if status3 != http.StatusOK || fleet["replicas"] != float64(6) {
		t.Fatalf("fleet after scale up: %d %v", status3, fleet)
	}
	_ = clk
}

func act(m map[string]any) float64 {
	if v, ok := m["applied_replicas"].(float64); ok {
		return v
	}
	return -1
}

// Bad input returns a specific error category envelope, not a 500.
func TestHTTPValidationErrors(t *testing.T) {
	srv, _, _ := setup(t, 1)
	defer srv.Close()

	status, body := do(t, "POST", srv.URL+"/v1/instances/ins-0001/samples", `{"value":-5}`, nil)
	if status != http.StatusBadRequest || body["code"] != "BAD_VALUE" {
		t.Fatalf("negative value: status=%d body=%v", status, body)
	}
	status, body = do(t, "POST", srv.URL+"/v1/instances/ins-0001/samples", `{not json`, nil)
	if status != http.StatusBadRequest || body["code"] != "BAD_JSON" {
		t.Fatalf("bad json: status=%d body=%v", status, body)
	}
	status, body = do(t, "POST", srv.URL+"/v1/demand", `{"pending":-1}`, nil)
	if status != http.StatusBadRequest || body["code"] != "BAD_PENDING" {
		t.Fatalf("bad pending: status=%d body=%v", status, body)
	}
}

// An invalid config PUT is rejected with INVALID_CONFIG and the prior config
// remains in force.
func TestHTTPInvalidConfigRejected(t *testing.T) {
	srv, _, _ := setup(t, 1)
	defer srv.Close()

	status, body := do(t, "PUT", srv.URL+"/config", `{"target_load_per_instance":0}`, nil)
	if status != http.StatusBadRequest || body["code"] != "INVALID_CONFIG" {
		t.Fatalf("invalid config: status=%d body=%v", status, body)
	}
	status, cur := do(t, "GET", srv.URL+"/config", "", nil)
	if status != http.StatusOK {
		t.Fatalf("get config: %d", status)
	}
	cfgMap := cur["config"].(map[string]any)
	if cfgMap["target_load_per_instance"].(float64) != 100 {
		t.Fatalf("config should be unchanged, got %v", cfgMap["target_load_per_instance"])
	}
}

// Scale from zero over HTTP: a fresh demand bootstraps one instance.
func TestHTTPZeroBootstrap(t *testing.T) {
	srv, _, _ := setup(t, 0)
	defer srv.Close()

	rid := "req-http-zero"
	status, _ := do(t, "POST", srv.URL+"/v1/demand", `{"pending":3}`, &rid)
	if status != http.StatusAccepted {
		t.Fatalf("demand ingest status=%d", status)
	}
	rec := "req-http-zero-rec"
	status, body := do(t, "POST", srv.URL+"/v1/reconcile", "", &rec)
	if status != http.StatusOK || body["action"] != "scale_up" || act(body) != float64(1) {
		t.Fatalf("bootstrap: status=%d action=%v applied=%v", status, body["action"], act(body))
	}
	_ = model.Reason{}
}
