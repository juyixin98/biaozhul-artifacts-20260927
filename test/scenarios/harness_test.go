// Package scenarios contains independent, black-box end-to-end tests. They
// exercise the three running components only over HTTP and inspect physical
// ownership through the actual service's own admin/database surface. The
// tests never call the reconciler's internal functions, and their expected
// outcomes (generations, counts, failure categories, ownership at each
// phase) are asserted independently of the controller's own ledger.
package scenarios

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"crcontroller/internal/actual"
	"crcontroller/internal/actualserver"
	"crcontroller/internal/actualstore"
	"crcontroller/internal/apiserver"
	"crcontroller/internal/controllerstore"
	"crcontroller/internal/desired"
	"crcontroller/internal/logx"
	"crcontroller/internal/reconcile"
	"crcontroller/internal/store"
)

const controllerAuth = "test-secret"

// env is a fully wired local deployment.
type env struct {
	t          *testing.T
	desiredURL string
	actualURL  string
	diagURL    string

	desiredDB *store.Store
	actualDB  *actualstore.Store
	ctlDB     *controllerstore.Store

	desiredSrv *httptest.Server
	actualSrv  *httptest.Server
	diagSrv    *httptest.Server

	ctl    *reconcile.Controller
	client *http.Client
	logger *logx.Logger
}

func newEnv(t *testing.T) *env {
	t.Helper()
	dir := t.TempDir()
	logger := logx.New(io.Discard)

	desiredDB, err := store.Open(filepath.Join(dir, "desired.db"))
	if err != nil {
		t.Fatalf("desired db: %v", err)
	}
	actualDB, err := actualstore.Open(filepath.Join(dir, "actual.db"))
	if err != nil {
		t.Fatalf("actual db: %v", err)
	}
	ctlDB, err := controllerstore.Open(filepath.Join(dir, "controller.db"))
	if err != nil {
		t.Fatalf("controller db: %v", err)
	}

	// Wire the API server with the controller's queue as the event sink.
	var ctl *reconcile.Controller
	apiSrv := apiserver.New(desiredDB, logger,
		apiserver.WithControllerAuth(controllerAuth),
		apiserver.WithEventSink(func(uid string) {
			if ctl != nil {
				ctl.Enqueue(uid)
			}
		}),
	)
	actualSrv := actualserver.New(actualDB, logger)

	desiredTS := httptest.NewServer(apiSrv.Handler())
	actualTS := httptest.NewServer(actualSrv.Handler())

	ctl = reconcile.New(reconcile.Config{
		Desired:        desired.New(desiredTS.URL, controllerAuth),
		Actual:         actual.New(actualTS.URL),
		Store:          ctlDB,
		Log:            logger,
		Backoff:        reconcile.Backoff{Base: 10 * time.Millisecond, Max: 200 * time.Millisecond},
		ResyncInterval: 24 * time.Hour, // tests drive events explicitly
		RequeueDelay:   5 * time.Millisecond,
	})
	ctx, cancel := context.WithCancel(context.Background())
	ctl.Start(ctx)

	diagHandler := reconcile.NewDiagnosticsServer(ctlDB, ctl)
	diagTS := httptest.NewServer(diagHandler.Handler())

	e := &env{
		t: t, desiredURL: desiredTS.URL, actualURL: actualTS.URL,
		diagURL:   diagTS.URL,
		desiredDB: desiredDB, actualDB: actualDB, ctlDB: ctlDB,
		desiredSrv: desiredTS, actualSrv: actualTS, diagSrv: diagTS,
		ctl: ctl, client: http.DefaultClient, logger: logger,
	}
	t.Cleanup(func() {
		cancel()
		ctl.Queue().ShutDown()
		desiredTS.Close()
		actualTS.Close()
		diagTS.Close()
		desiredDB.Close()
		actualDB.Close()
		ctlDB.Close()
	})
	return e
}

// ---- HTTP helpers ----

func (e *env) do(method, url string, body any, headers map[string]string) (int, map[string]any) {
	e.t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		e.t.Fatalf("request: %v", err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := e.client.Do(req)
	if err != nil {
		e.t.Fatalf("http %s %s: %v", method, url, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	if out == nil {
		out = map[string]any{}
	}
	out["__status"] = float64(resp.StatusCode)
	out["__requestID"] = resp.Header.Get("X-Request-ID")
	return resp.StatusCode, out
}

func (e *env) controllerHeaders() map[string]string {
	return map[string]string{"X-Controller-Auth": controllerAuth}
}

// createResource POSTs a new resource and returns its UID.
func (e *env) createResource(ns, name string, spec map[string]any) string {
	status, body := e.do(http.MethodPost,
		e.desiredURL+"/api/v1/namespaces/"+ns+"/resources",
		map[string]any{"name": name, "spec": spec}, nil)
	if status != http.StatusCreated {
		e.t.Fatalf("create status=%d body=%v", status, body)
	}
	return body["uid"].(string)
}

func (e *env) resourceURL(ns, name string) string {
	return e.desiredURL + "/api/v1/namespaces/" + ns + "/resources/" + name
}

func (e *env) getResource(ns, name string) map[string]any {
	status, body := e.do(http.MethodGet, e.resourceURL(ns, name), nil, nil)
	if status != http.StatusOK {
		e.t.Fatalf("get %s/%s status=%d body=%v", ns, name, status, body)
	}
	return body
}

func (e *env) getResourcePriv(ns, name string) map[string]any {
	status, body := e.do(http.MethodGet, e.resourceURL(ns, name), nil,
		e.controllerHeaders())
	if status != http.StatusOK {
		e.t.Fatalf("get priv %s/%s status=%d", ns, name, status)
	}
	return body
}

func (e *env) updateSpec(ns, name string, rv int64, spec map[string]any) (int, map[string]any) {
	h := e.controllerHeaders()
	h["If-Match"] = fmt.Sprintf("%d", rv)
	return e.do(http.MethodPut, e.resourceURL(ns, name),
		map[string]any{"spec": spec}, h)
}

func (e *env) deleteResource(ns, name string) (int, map[string]any) {
	return e.do(http.MethodDelete, e.resourceURL(ns, name), nil, nil)
}

func (e *env) nudge(uid string) {
	e.do(http.MethodPost, e.desiredURL+"/api/v1/events",
		map[string]any{"uid": uid, "type": "manual"}, nil)
}

// ---- fault injection ----

func (e *env) setActualFault(ownerUID, fault string) {
	status, body := e.do(http.MethodPost, e.actualURL+"/admin/faults",
		map[string]any{"ownerUID": ownerUID, "fault": fault}, nil)
	if status != http.StatusOK {
		e.t.Fatalf("set actual fault: %d %v", status, body)
	}
}

func (e *env) clearActualFault(ownerUID string) {
	status, _ := e.do(http.MethodDelete,
		e.actualURL+"/admin/faults/"+ownerUID, nil, nil)
	if status != http.StatusNoContent && status != http.StatusNotFound {
		e.t.Fatalf("clear actual fault: %d", status)
	}
}

func (e *env) setDesiredFault(uid, fault string) {
	status, body := e.do(http.MethodPost, e.desiredURL+"/admin/faults",
		map[string]any{"uid": uid, "fault": fault}, nil)
	if status != http.StatusOK {
		e.t.Fatalf("set desired fault: %d %v", status, body)
	}
}

// ---- independent oracle ----

func (e *env) actualResourcesByOwner(ownerUID string) []map[string]any {
	resp, err := http.Get(e.actualURL + "/admin/resources")
	if err != nil {
		e.t.Fatalf("list actual: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var all []map[string]any
	if err := json.Unmarshal(raw, &all); err != nil {
		e.t.Fatalf("list actual decode: %v: %s", err, string(raw))
	}
	var out []map[string]any
	for _, m := range all {
		if m["ownerUID"] == ownerUID {
			out = append(out, m)
		}
	}
	return out
}

func (e *env) actualCounters() map[string]any {
	status, body := e.do(http.MethodGet, e.actualURL+"/admin/counters", nil, nil)
	if status != http.StatusOK {
		e.t.Fatalf("counters: %d", status)
	}
	delete(body, "__status")
	delete(body, "__requestID")
	return body
}

type ledgerEntry = map[string]any

// rawLedger fetches the ledger as the bare JSON array diagnostics serves.
func (e *env) rawLedger(uid string) []map[string]any {
	resp, err := http.Get(e.diagURL + "/diagnostics/ledger/" + uid + "?limit=2000")
	if err != nil {
		e.t.Fatalf("rawLedger: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var items []map[string]any
	if err := json.Unmarshal(raw, &items); err != nil {
		e.t.Fatalf("rawLedger decode: %v: %s", err, string(raw))
	}
	return items
}

func (e *env) ledgerDecisions(uid string) []map[string]any {
	return e.rawLedger(uid)
}

// ---- assertions / waiting ----

func waitFor(deadline time.Duration, fn func() bool) bool {
	deadlineAt := time.Now().Add(deadline)
	for time.Now().Before(deadlineAt) {
		if fn() {
			return true
		}
		time.Sleep(15 * time.Millisecond)
	}
	return fn()
}

func asString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func asFloat(v any) float64 {
	if f, ok := v.(float64); ok {
		return f
	}
	return 0
}

func countCategory(entries []map[string]any, cat string) int {
	n := 0
	for _, m := range entries {
		if asString(m["category"]) == cat {
			n++
		}
	}
	return n
}

func countDecision(entries []map[string]any, dec string) int {
	n := 0
	for _, m := range entries {
		if asString(m["decision"]) == dec {
			n++
		}
	}
	return n
}
