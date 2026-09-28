// Package integration contains end-to-end tests that boot the full
// application — config file, file-backed SQLite, the ordered chain built by
// the config factory, the background reconciler and the stdlib HTTP server —
// and drive it over real HTTP.
//
// Expected final objects and per-step patches are asserted literally in these
// tests: the reference answers live in the tests, not in the code under test.
package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"admission/internal/config"
	"admission/internal/httpapi"
	"admission/internal/reconcile"
	"admission/internal/runlogger"
	"admission/internal/service"
	"admission/internal/storage"
	"admission/internal/types"
)

// app is a booted, fully wired instance.
type app struct {
	baseURL string
	store   *storage.SQLiteStore
	logger  *runlogger.Logger
	logDir  string
	cfgFile string
	cancel  context.CancelFunc
	client  *http.Client
}

func bootApp(t *testing.T, cfgBody string) *app {
	t.Helper()
	dir := t.TempDir()
	cfgBody = strings.ReplaceAll(cfgBody, "PLACEHOLDER_DB", filepath.Join(dir, "admission.db"))
	cfgBody = strings.ReplaceAll(cfgBody, "PLACEHOLDER_LOGS", filepath.Join(dir, "logs"))

	cfgPath := filepath.Join(dir, "config.json")
	if err := os.WriteFile(cfgPath, []byte(cfgBody), 0o644); err != nil {
		t.Fatal(err)
	}
	f, err := config.Load(cfgPath)
	if err != nil {
		t.Fatalf("config load: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	store, err := storage.Open(ctx, f.Storage.SQLitePath)
	if err != nil {
		t.Fatalf("storage open: %v", err)
	}
	logger := runlogger.New(f.Logging.Dir)
	built, err := config.Build(f, logger)
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	svc, err := service.New(service.Deps{Pipeline: built.Pipeline, Store: store, Ledger: built.Ledger})
	if err != nil {
		t.Fatalf("service: %v", err)
	}
	rec := reconcile.New(svc, store, reconcile.Policy{
		MaxAttempts: f.Reconcile.MaxAttempts,
		BaseDelay:   time.Duration(f.Reconcile.BaseDelayMS) * time.Millisecond,
		Factor:      f.Reconcile.Factor,
		MaxDelay:    time.Duration(f.Reconcile.MaxDelayMS) * time.Millisecond,
	}, nil, nil)
	go rec.Start(ctx, time.Duration(f.Reconcile.TickMS)*time.Millisecond)

	srv := httpapi.NewServer(svc, rec, logger).WithWires(store.RecentAudit, store.CountRetry)
	httpSrv := &http.Server{Handler: srv.Handler()}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	go func() { _ = httpSrv.Serve(ln) }()
	t.Cleanup(func() {
		cancel()
		shCtx, c := context.WithTimeout(context.Background(), time.Second)
		defer c()
		_ = httpSrv.Shutdown(shCtx)
		_ = store.Close()
	})

	return &app{
		baseURL: "http://" + ln.Addr().String(),
		store:   store,
		logger:  logger,
		logDir:  f.Logging.Dir,
		cfgFile: cfgPath,
		cancel:  cancel,
		client:  &http.Client{Timeout: 5 * time.Second},
	}
}

// admissionBody builds the wire request body.
func admissionBody(uid, op string, obj types.Object, old *types.Object, dryRun bool) map[string]any {
	inner := map[string]any{
		"uid": uid, "operation": op, "object": obj, "dryRun": dryRun,
	}
	if old != nil {
		inner["oldObject"] = old
	}
	return map[string]any{
		"apiVersion": "admission.example.com/v1",
		"kind":       "AdmissionReview",
		"request":    inner,
	}
}

func workload(name, team string) types.Object {
	return types.Object{
		APIVersion: "apps.example.com/v1",
		Kind:       "Workload",
		Metadata:   types.Metadata{Namespace: "team-a", Name: name, Labels: map[string]string{"team": team}},
		Spec:       types.Spec{CPU: "500m", Memory: "128Mi"},
	}
}

func (a *app) postAdmission(t *testing.T, body map[string]any) (int, httpapi.AdmissionResponse) {
	t.Helper()
	b, _ := json.Marshal(body)
	resp, err := a.client.Post(a.baseURL+"/admission", "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatalf("POST /admission: %v", err)
	}
	defer resp.Body.Close()
	var ar httpapi.AdmissionResponse
	if err := json.NewDecoder(resp.Body).Decode(&ar); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return resp.StatusCode, ar
}

func (a *app) getQueue(t *testing.T) int {
	resp, err := a.client.Get(a.baseURL + "/retry/queue")
	if err != nil {
		t.Fatalf("GET queue: %v", err)
	}
	defer resp.Body.Close()
	var out struct {
		Pending int `json:"pending"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out.Pending
}

func (a *app) getAudit(t *testing.T) []types.AuditEvent {
	resp, err := a.client.Get(a.baseURL + "/audit/recent")
	if err != nil {
		t.Fatalf("GET audit: %v", err)
	}
	defer resp.Body.Close()
	var out struct {
		Events []types.AuditEvent `json:"events"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out.Events
}

// standardConfig is the production-shaped chain used by the happy-path tests.
const standardConfig = `{
  "storage": {"sqlitePath": "PLACEHOLDER_DB"},
  "logging": {"dir": "PLACEHOLDER_LOGS"},
  "admission": {
    "defaultTimeoutMs": 250,
    "maxMutationPasses": 3,
    "defaults": [
      {"type": "defaults.replicas", "failurePolicy": "FailClose", "args": {"default": 3}},
      {"type": "defaults.resources", "failurePolicy": "FailClose"}
    ],
    "mutators": [
      {"type": "mutators.reserved-resources", "failurePolicy": "FailClose"},
      {"type": "mutators.label-sync", "failurePolicy": "FailClose"}
    ],
    "validators": [
      {"type": "validators.replica-range", "failurePolicy": "FailClose", "args": {"min": 1, "max": 10}},
      {"type": "validators.immutable-fields", "failurePolicy": "FailClose"},
      {"type": "validators.quota", "failurePolicy": "FailClose",
       "args": {"capacityCPUm": 2000, "capacityMemoryBytes": 2147483648}}
    ]
  },
  "reconcile": {"maxAttempts": 4, "baseDelayMs": 20, "factor": 2, "maxDelayMs": 200, "tickMs": 10}
}`
