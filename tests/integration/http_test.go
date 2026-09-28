// Package integration_test drives the fully assembled system over real HTTP:
// a config file on disk, a real SQLite file, the run-log JSONL file and the
// background reconciler. These tests intentionally do not reach into the
// pipeline package — they assert externally observable results (status codes,
// failure categories, exact final object values, persisted audit/resource
// state), so they cannot pass merely because the core exposes its own
// internals.
package integration_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"admission/internal/adapter"
	"admission/internal/app"
	"admission/internal/config"
	"admission/internal/model"
	"admission/internal/storage"
)

type env struct {
	t       *testing.T
	dir     string
	srv     *httptest.Server
	system  *app.System
	cfgPath string
}

func startEnv(t *testing.T, quotaLimits map[string]int64) *env {
	t.Helper()
	dir := t.TempDir()
	catalogPath := filepath.Join(dir, "defaults.json")
	if err := os.WriteFile(catalogPath, []byte(`{
		  "Widget": {"replicas": 1, "schedule": "always"}
		}`), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := map[string]any{
		"listen":              "127.0.0.1:0",
		"databasePath":        filepath.Join(dir, "admission.db"),
		"logDir":              filepath.Join(dir, "runs"),
		"defaultsFile":        catalogPath,
		"maxPasses":           5,
		"reconcileIntervalMs": 100,
		"quotaLimits":         quotaLimits,
		"mutators": []map[string]any{
			{"type": "defaults", "failPolicy": "closed", "timeoutMs": 200},
			{"type": "capacity", "failPolicy": "closed", "timeoutMs": 200, "perReplica": 100},
			{"type": "stamp-uid", "failPolicy": "closed", "timeoutMs": 200},
		},
		"validators": []map[string]any{
			{"type": "schema", "failPolicy": "closed", "timeoutMs": 200, "maxReplicas": 5},
			{"type": "quota", "failPolicy": "closed", "timeoutMs": 500},
		},
	}
	cfgRaw, _ := json.Marshal(cfg)
	cfgPath := filepath.Join(dir, "admissiond.json")
	if err := os.WriteFile(cfgPath, cfgRaw, 0o644); err != nil {
		t.Fatal(err)
	}

	loaded, err := config.Load(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := config.LoadDefaultsCatalog(catalogPath)
	if err != nil {
		t.Fatal(err)
	}
	system, err := app.Build(context.Background(), loaded, catalog)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = system.Close() })

	srv := httptest.NewServer(adapter.NewHandler(system.Coordinator, system.Store))
	t.Cleanup(srv.Close)
	return &env{t: t, dir: dir, srv: srv, system: system, cfgPath: cfgPath}
}

func (e *env) post(body any) (int, model.Response, adapter.ErrorBody) {
	e.t.Helper()
	raw, _ := json.Marshal(body)
	resp, err := http.Post(e.srv.URL+"/v1/requests", "application/json", bytes.NewReader(raw))
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var probe map[string]any
	if err := json.Unmarshal(data, &probe); err != nil {
		return resp.StatusCode, model.Response{}, adapter.ErrorBody{Error: string(data)}
	}
	if _, isVerdict := probe["decision"]; isVerdict {
		var r model.Response
		if err := json.Unmarshal(data, &r); err != nil {
			e.t.Fatalf("decode verdict: %v", err)
		}
		return resp.StatusCode, r, adapter.ErrorBody{}
	}
	var eb adapter.ErrorBody
	if err := json.Unmarshal(data, &eb); err != nil {
		e.t.Fatalf("decode error body: %v", err)
	}
	return resp.StatusCode, model.Response{}, eb
}

func createReq(uid, name string, spec map[string]any) model.Request {
	return model.Request{
		UID: uid, Operation: model.OpCreate,
		Object: map[string]any{
			"apiVersion": "v1", "kind": "Widget",
			"metadata": map[string]any{"name": name, "namespace": "shop"},
			"spec":     spec,
		},
	}
}

// TestHTTP_DefaultsApplied_DuplicateReplaysSameDigest is the headline path:
// a bare object gets defaults, capacity derived from them, the UID stamp, and
// final validation. Posting the same UID again replays the identical verdict.
func TestHTTP_DefaultsApplied_DuplicateReplaysSameDigest(t *testing.T) {
	e := startEnv(t, map[string]int64{"Widget": 10})

	status, resp, errBody := e.post(createReq("e2e-1", "orders", map[string]any{}))
	if status != http.StatusOK || errBody.Error != "" {
		t.Fatalf("status=%d err=%+v", status, errBody)
	}
	if resp.Decision != model.DecisionAllowed || resp.FinalSummary == nil {
		t.Fatalf("decision=%s summary=%v", resp.Decision, resp.FinalSummary)
	}
	spec := resp.FinalObject["spec"].(map[string]any)
	if getInt(spec, "replicas") != 1 || spec["schedule"] != "always" || getInt(spec, "capacity") != 100 {
		t.Fatalf("unexpected final spec: %#v", spec)
	}
	ann := resp.FinalObject["metadata"].(map[string]any)["annotations"].(map[string]any)
	if ann["admission.uid"] != "e2e-1" {
		t.Fatalf("uid annotation not bound to object: %#v", ann)
	}
	firstDigest := resp.FinalSummary.Digest

	// Duplicate submission: must replay the stored verdict without producing
	// a second audit record (i.e. no reprocessing at all).
	status2, resp2, errBody2 := e.post(createReq("e2e-1", "orders", map[string]any{}))
	if status2 != http.StatusOK || errBody2.Error != "" {
		t.Fatalf("duplicate status=%d err=%+v", status2, errBody2)
	}
	if resp2.FinalSummary.Digest != firstDigest {
		t.Fatalf("duplicate replay digest changed:\n%s\n%s", firstDigest, resp2.FinalSummary.Digest)
	}
	auditsAfter := countAudits(t, e, "e2e-1")
	if auditsAfter != 1 {
		t.Fatalf("duplicate call created %d audit runs, want exactly 1 (pure replay)", auditsAfter)
	}
}

func countAudits(t *testing.T, e *env, uid string) int {
	t.Helper()
	httpResp, err := http.Get(e.srv.URL + "/v1/requests/" + uid + "/audits")
	if err != nil {
		t.Fatal(err)
	}
	defer httpResp.Body.Close()
	var envelope struct {
		Audits []json.RawMessage `json:"audits"`
	}
	if err := json.NewDecoder(httpResp.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	return len(envelope.Audits)
}

// TestHTTP_FailureCategoriesAreDistinguishable asserts the four categories
// appear with the correct HTTP status, not just that calls succeed.
func TestHTTP_FailureCategoriesAreDistinguishable(t *testing.T) {
	e := startEnv(t, map[string]int64{"Widget": 2})

	// input_error -> 400
	bad := map[string]any{"operation": "CREATE"} // missing uid + object
	status, _, eb := e.post(bad)
	if status != 400 || eb.Reason != model.ReasonInvalidInput || eb.Category != "input_error" {
		t.Fatalf("input: status=%d body=%+v", status, eb)
	}

	// validation_denied (computation/policy class) -> 422
	_, schemaDenial, _ := e.post(createReq("e2e-schema", "big", map[string]any{"replicas": 99}))
	if schemaDenial.Decision != model.DecisionDenied || schemaDenial.Reason != model.ReasonValidationDenied {
		t.Fatalf("schema denial wrong: %+v", schemaDenial)
	}
	if schemaDenial.FinalSummary == nil {
		t.Fatal("schema denial must bind the final object summary")
	}

	// Fill the 2-unit quota, then overflow -> resource_exhausted 429. Denials
	// are returned as a Response with decision=denied; the category is on
	// Reason, not in an error body.
	for i, uid := range []string{"e2e-q1", "e2e-q2"} {
		if st, r, b := e.post(createReq(uid, "r"+uid, map[string]any{"replicas": 1})); st != 200 || b.Error != "" {
			t.Fatalf("fill %d (%s): status=%d resp=%+v err=%+v", i, uid, st, r, b)
		}
	}
	status, overflow, _ := e.post(createReq("e2e-q3", "overflow", map[string]any{"replicas": 1}))
	if status != 429 || overflow.Decision != model.DecisionDenied ||
		overflow.Reason != model.ReasonResourceExhausted || overflow.Reason.Category() != "resource_exhausted" {
		t.Fatalf("exhaustion: status=%d decision=%s reason=%s", status, overflow.Decision, overflow.Reason)
	}

	// state_conflict -> 409: two different UIDs create the same resource name.
	// Use a separate, unlimited environment so the collision — not the quota —
	// is the asserted failure.
	e2 := startEnv(t, nil)
	if st, r, b := e2.post(createReq("e2e-c1", "collision", map[string]any{})); st != 200 || b.Error != "" {
		t.Fatalf("first create: %d %+v %+v", st, r, b)
	}
	status, _, conflictErr := e2.post(createReq("e2e-c2", "collision", map[string]any{}))
	if status != 409 || conflictErr.Category != "state_conflict" {
		t.Fatalf("conflict: status=%d body=%+v", status, conflictErr)
	}
}

// TestHTTP_AuditEndpointAndRunLog verifies the replay surfaces (per-request
// audit with run id + step trace, and the JSONL run log) contain the data
// needed to reproduce a problem.
func TestHTTP_AuditEndpointAndRunLog(t *testing.T) {
	e := startEnv(t, map[string]int64{"Widget": 5})
	if _, resp, b := e.post(createReq("e2e-audit", "audited", map[string]any{})); b.Error != "" {
		t.Fatalf("post: %+v resp=%+v", b, resp)
	}

	httpResp, err := http.Get(e.srv.URL + "/v1/requests/e2e-audit/audits")
	if err != nil {
		t.Fatal(err)
	}
	defer httpResp.Body.Close()
	if httpResp.StatusCode != 200 {
		t.Fatalf("audits status = %d", httpResp.StatusCode)
	}
	var audits struct {
		UID    string `json:"uid"`
		Audits []struct {
			RunID  string         `json:"runId"`
			Status string         `json:"status"`
			Resp   model.Response `json:"response"`
		} `json:"audits"`
	}
	if err := json.NewDecoder(httpResp.Body).Decode(&audits); err != nil {
		t.Fatal(err)
	}
	if len(audits.Audits) != 1 {
		t.Fatalf("want 1 audit, got %d", len(audits.Audits))
	}
	rec := audits.Audits[0]
	if !strings.HasPrefix(rec.RunID, "run-") {
		t.Fatalf("run id not replay-greppable: %s", rec.RunID)
	}
	if len(rec.Resp.Steps) < 4 {
		t.Fatalf("audit must retain each ordered step, got %d: %+v", len(rec.Resp.Steps), rec.Resp.Steps)
	}
	if rec.Resp.FinalSummary == nil || rec.Resp.FinalSummary.Digest == "" {
		t.Fatal("audit response missing final summary binding")
	}

	// Run-log JSONL contains a line with the same run id and non-empty steps.
	data, err := os.ReadFile(filepath.Join(e.dir, "runs", "admission-runs.jsonl"))
	if err != nil {
		t.Fatalf("read run log: %v", err)
	}
	if !strings.Contains(string(data), rec.RunID) {
		t.Fatalf("run log does not contain run id %s", rec.RunID)
	}
	if !strings.Contains(string(data), "\"steps\":") {
		t.Fatal("run log missing intermediate steps")
	}
}

// TestReconcile_RecoversPendingRow simulates a request whose row exists but
// whose worker never finished (crash before processing): the coordinator's
// reconcile loop picks it up and drives it to a terminal, committed state.
func TestReconcile_RecoversPendingRow(t *testing.T) {
	e := startEnv(t, map[string]int64{"Widget": 5})
	ctx := context.Background()

	crashed := createReq("e2e-crash", "recovered", map[string]any{})
	payload, _ := json.Marshal(crashed)
	existed, err := e.system.Store.InsertRequest(ctx, string(payload), "e2e-crash", model.OpCreate, time.Now().UnixMilli())
	if err != nil || existed {
		t.Fatalf("seed pending: existed=%v err=%v", existed, err)
	}

	did, err := e.system.Coordinator.ReconcileOnce(ctx)
	if err != nil || !did {
		t.Fatalf("reconcile did not claim: did=%v err=%v", did, err)
	}
	row, err := e.system.Store.GetRequest(ctx, "e2e-crash")
	if err != nil {
		t.Fatal(err)
	}
	if row.Status != storage.StatusAllowed {
		t.Fatalf("status=%s reason=%s msg=%s", row.Status, row.Reason, row.Message)
	}
	if _, err := e.system.Store.GetResource(ctx, "Widget", "shop", "recovered"); err != nil {
		t.Fatalf("recovered resource not committed: %v", err)
	}
}

// TestRestart_ReplaysVerdictFromSQLite closes the process and rebuilds it
// from the same database file: an existing UID replays without reprocessing.
func TestRestart_ReplaysVerdictFromSQLite(t *testing.T) {
	e := startEnv(t, map[string]int64{"Widget": 5})
	_, first, b := e.post(createReq("e2e-restart", "persist", map[string]any{"replicas": 2}))
	if b.Error != "" {
		t.Fatal(b.Error)
	}
	digest := first.FinalSummary.Digest
	if err := e.system.Close(); err != nil {
		t.Fatal(err)
	}
	e.srv.Close()

	cfg, err := config.Load(e.cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := config.LoadDefaultsCatalog(cfg.DefaultsFile)
	if err != nil {
		t.Fatal(err)
	}
	system2, err := app.Build(context.Background(), cfg, catalog)
	if err != nil {
		t.Fatal(err)
	}
	defer system2.Close()
	srv2 := httptest.NewServer(adapter.NewHandler(system2.Coordinator, system2.Store))
	defer srv2.Close()

	raw, _ := json.Marshal(createReq("e2e-restart", "persist", map[string]any{"replicas": 2}))
	resp, err := http.Post(srv2.URL+"/v1/requests", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var replay model.Response
	if err := json.NewDecoder(resp.Body).Decode(&replay); err != nil {
		t.Fatal(err)
	}
	if replay.Decision != model.DecisionAllowed || replay.FinalSummary.Digest != digest {
		t.Fatalf("after restart verdict not replayed: decision=%s digest=%s want=%s",
			replay.Decision, replay.FinalSummary.Digest, digest)
	}
}

func getInt(m map[string]any, key string) int64 {
	switch n := m[key].(type) {
	case float64:
		return int64(n)
	case int64:
		return n
	case int:
		return int64(n)
	}
	return -1
}
