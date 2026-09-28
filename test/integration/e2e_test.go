package integration

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"admission/internal/types"
)

// E2E 1 — happy path over HTTP: defaults filled, mutually influencing plugins
// converge, final object and each recorded patch are asserted literally.
func TestE2EHappyPathDefaultsInteractionFinal(t *testing.T) {
	a := bootApp(t, standardConfig)
	status, ar := a.postAdmission(t, admissionBody("uid-e2e-1", "CREATE", workload("w1", "payments"), nil, false))
	if status != 200 {
		t.Fatalf("http status=%d", status)
	}
	r := ar.Response.Response
	if !r.Allowed {
		t.Fatalf("not allowed: cat=%s msg=%s", r.FailureCategory, r.Message)
	}

	// Final object: defaults + projection + synced annotations + immutable.
	if r.Object.Spec.Replicas == nil || *r.Object.Spec.Replicas != 3 {
		t.Fatalf("replicas=%v want 3", r.Object.Spec.Replicas)
	}
	if r.Object.Spec.CPU != "250m" && r.Object.Spec.CPU != "500m" {
		t.Fatalf("cpu=%s", r.Object.Spec.CPU)
	}
	// CPU was provided (500m), so the resource defaulter must not overwrite it.
	if r.Object.Spec.CPU != "500m" {
		t.Fatalf("provided cpu must win over default: %s", r.Object.Spec.CPU)
	}
	if r.Object.Spec.Memory != "128Mi" {
		t.Fatalf("memory=%s", r.Object.Spec.Memory)
	}
	if got := num(r.Object.Spec.Extra["reservedCPU"]); got != 500 {
		t.Fatalf("reservedCPU=%v want 500", r.Object.Spec.Extra)
	}
	if ann := r.Object.Metadata.Annotations; ann["admission.example.com/team"] != "payments" ||
		ann["admission.example.com/reserved-cpu-milli"] != "500" {
		t.Fatalf("synced annotations wrong: %v", ann)
	}
	if !r.Object.Spec.Immutable {
		t.Fatal("admitted object must be immutable")
	}
	if r.Final.Fingerprint != r.Object.Fingerprint() {
		t.Fatal("response final summary not bound to final object")
	}

	// Per-step patches: collect mutating steps and assert the exact paths, in
	// chain order.
	var patchPaths []string
	for _, s := range r.Steps {
		for _, op := range s.Patch {
			patchPaths = append(patchPaths, string(s.Phase)+":"+s.Plugin+":"+string(op.Op)+" "+op.Path)
		}
	}
	joined := strings.Join(patchPaths, "\n")
	for _, want := range []string{
		"default:defaults.replicas:add /spec/replicas",
		"mutating:mutators.reserved-resources:add /spec/extra/reservedCPU",
		"mutating:mutators.label-sync:add /metadata/annotations/admission.example.com~1team",
		"mutating:mutators.label-sync:add /metadata/annotations/admission.example.com~1reserved-cpu-milli",
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("step patches:\n%s\n-- missing %q", joined, want)
		}
	}
	// No mutating step may have run on the convergence pass.
	for _, s := range r.Steps {
		if s.Phase == types.PhaseMutating && s.Pass == 2 && s.Mutated {
			t.Fatalf("convergence pass still mutated: %s %+v", s.Plugin, s.Patch)
		}
	}

	// Audit endpoint serves the bound event.
	events := a.getAudit(t)
	if len(events) == 0 || events[0].UID != "uid-e2e-1" {
		t.Fatalf("audit missing event: %+v", events)
	}
	if events[0].Final.Fingerprint != r.Object.Fingerprint() {
		t.Fatal("audit final summary not bound to final object")
	}
}

// E2E 2 — duplicate call over HTTP returns the stored verdict and does not
// re-run the chain or re-book capacity.
func TestE2EDuplicateCallReplays(t *testing.T) {
	a := bootApp(t, standardConfig)
	obj := workload("w2", "billing")
	_, first := a.postAdmission(t, admissionBody("uid-dup", "CREATE", obj, nil, false))
	if !first.Response.Allowed {
		t.Fatal("first call failed")
	}
	firstFP := first.Response.Object.Fingerprint()

	// Second call with a different object body but same UID.
	obj2 := obj
	obj2.Spec.CPU = "900m"
	_, second := a.postAdmission(t, admissionBody("uid-dup", "CREATE", obj2, nil, false))
	if !second.Response.Replayed {
		t.Fatal("duplicate must set replayed=true")
	}
	if second.Response.Object.Fingerprint() != firstFP {
		t.Fatal("replayed response must return stored object")
	}
	if second.Response.RunID == first.Response.RunID {
		t.Fatal("replay must carry the duplicate call's own run id for tracing")
	}
	// Exactly one stored verdict.
	if resp, ok, _ := a.store.LookupUID(context.Background(), "uid-dup"); !ok || !resp.Allowed {
		t.Fatalf("stored verdict wrong: ok=%v allowed=%v", ok, resp.Allowed)
	}
}

// E2E 3 — illegal path mutation returns the IllegalMutation category and the
// object is unchanged; verdict is terminal (queue drains, not retried).
func TestE2EIllegalMutation(t *testing.T) {
	cfg := injectPlugin(standardConfig, `
      {"type": "fixtures.rogue", "name": "rogue.e2e", "failurePolicy": "FailOpen",
       "args": {"attack": "outside"}}`)
	a := bootApp(t, cfg)

	_, ar := a.postAdmission(t, admissionBody("uid-rogue", "CREATE", workload("w3", "x"), nil, false))
	r := ar.Response.Response
	if r.Allowed || r.FailureCategory != types.CatIllegalMutation {
		t.Fatalf("expected IllegalMutation, got allowed=%v cat=%s", r.Allowed, r.FailureCategory)
	}
	if r.FailedPlugin != "rogue.e2e" {
		t.Fatalf("failedPlugin=%s", r.FailedPlugin)
	}
	if r.Object.Spec.Replicas != nil {
		t.Fatal("no partial mutation may be committed")
	}
	time.Sleep(60 * time.Millisecond)
	if n := a.getQueue(t); n != 0 {
		t.Fatalf("illegal mutation must be terminal, queue=%d", n)
	}
}

// E2E 4 — hard-guarded immutable path is refused even if the plugin claims
// /spec as its prefix (rogue does not, but the guard is independent).
func TestE2EImmutableGuard(t *testing.T) {
	cfg := injectPlugin(standardConfig, `
      {"type": "fixtures.rogue", "name": "rogue.guard", "failurePolicy": "FailClose",
       "args": {"attack": "immutable"}}`)
	a := bootApp(t, cfg)
	_, ar := a.postAdmission(t, admissionBody("uid-guard", "CREATE", workload("w4", "x"), nil, false))
	if ar.Response.FailureCategory != types.CatIllegalMutation ||
		!strings.Contains(ar.Response.Message, "hard-guarded") {
		t.Fatalf("expected guarded IllegalMutation, got %+v", ar.Response)
	}
}

// E2E 5 — timeout fixture over HTTP: category Timeout, plugin named, and the
// queue drains because the config marks the timeout FailClose but timeout is
// retryable... here the plugin is configured FailClose, yet the category is
// still Timeout and distinguishable; reconciliation retries and keeps failing
// until the attempt cap, after which the queue is empty.
func TestE2ETimeoutRetriedToCap(t *testing.T) {
	cfg := injectPlugin(standardConfig, `
      {"type": "fixtures.delay", "name": "slow.e2e", "failurePolicy": "FailClose",
       "timeoutMs": 20, "args": {"delayMs": 1000}}`)
	a := bootApp(t, cfg)

	_, ar := a.postAdmission(t, admissionBody("uid-slow", "CREATE", workload("w5", "x"), nil, false))
	r := ar.Response.Response
	if r.FailureCategory != types.CatTimeout {
		t.Fatalf("expected Timeout, got cat=%s msg=%s", r.FailureCategory, r.Message)
	}
	if r.FailedPlugin != "slow.e2e" {
		t.Fatalf("plugin=%s", r.FailedPlugin)
	}

	// Wait on the durable OUTCOME (audit attempts), not the transient queue
	// depth: an item is momentarily invisible while a tick has dequeued it and
	// the 20ms timeout attempt is executing.
	waitForAttempts(t, a, "uid-slow", types.CatTimeout, 4, 3*time.Second)
	if n := a.getQueue(t); n != 0 {
		t.Fatalf("queue should drain after attempt cap, pending=%d", n)
	}
	events := a.getAudit(t)
	var timeouts int
	for _, ev := range events {
		if ev.UID == "uid-slow" && ev.Category == types.CatTimeout {
			timeouts++
		}
	}
	if timeouts != 4 {
		t.Fatalf("expected 4 timeout attempts in audit, got %d", timeouts)
	}

	// Replay logs: four run files (initial HTTP run + three reconciler runs),
	// each carrying its own run id and the structured Timeout category/reason.
	entries, err := os.ReadDir(a.logDir)
	if err != nil {
		t.Fatalf("log dir: %v", err)
	}
	var runFiles, timeoutRecords int
	var combined string
	for _, e := range entries {
		if !strings.HasPrefix(e.Name(), "run-") {
			continue
		}
		runFiles++
		b, err := os.ReadFile(filepath.Join(a.logDir, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		s := string(b)
		combined += s
		if strings.Contains(s, "slow.e2e") {
			timeoutRecords += strings.Count(s, `"category":"Timeout"`)
		}
	}
	if runFiles < 4 {
		t.Fatalf("expected at least 4 per-run log files, got %d", runFiles)
	}
	if timeoutRecords != 4 {
		t.Fatalf("expected 4 Timeout records across logs, got %d; combined:\n%s", timeoutRecords, combined)
	}
	if !strings.Contains(combined, "deadline") {
		t.Fatal("replay log must retain the timeout reason (deadline)")
	}
}

// E2E 6 — input errors are terminal and distinguishable from quota exhaustion:
// two bad requests, each stored with its own category.
func TestE2EErrorCategoriesDistinct(t *testing.T) {
	a := bootApp(t, standardConfig)

	bad := workload("w6", "x")
	bad.Spec.CPU = "nonsense"
	_, ar1 := a.postAdmission(t, admissionBody("uid-badinput", "CREATE", bad, nil, false))
	if ar1.Response.FailureCategory != types.CatInvalidInput {
		t.Fatalf("expected InvalidInput, got %s", ar1.Response.FailureCategory)
	}

	big := workload("w7", "x")
	big.Spec.CPU = "5000m" // exceeds 2000m capacity
	_, ar2 := a.postAdmission(t, admissionBody("uid-big", "CREATE", big, nil, false))
	if ar2.Response.FailureCategory != types.CatQuotaExhausted {
		t.Fatalf("expected QuotaExhausted, got %s (%s)", ar2.Response.FailureCategory, ar2.Response.Message)
	}
	if !strings.Contains(ar2.Response.Message, "cpu") {
		t.Fatalf("quota message should identify resource: %s", ar2.Response.Message)
	}

	// Invalid input must not be retried; quota may be (but stays exhausted).
	time.Sleep(60 * time.Millisecond)
	events := a.getAudit(t)
	seen := map[string]int{}
	for _, ev := range events {
		seen[string(ev.Category)]++
	}
	if seen["InvalidInput"] == 0 || seen["QuotaExhausted"] == 0 {
		t.Fatalf("both categories must appear in audit: %v", seen)
	}
}

// E2E 7 — UPDATE of an immutable object that changes core fields is a
// StateConflict; an unchanged UPDATE is allowed.
func TestE2EImmutableUpdate(t *testing.T) {
	a := bootApp(t, standardConfig)
	// Create first (object becomes immutable post-admission).
	_, ar := a.postAdmission(t, admissionBody("uid-imm", "CREATE", workload("w8", "x"), nil, false))
	if !ar.Response.Allowed {
		t.Fatal(ar.Response.Message)
	}
	created := ar.Response.Object

	// Unchanged UPDATE -> allowed (duplicate UID check is keyed by request UID;
	// use a new UID for the update call with oldObject attached).
	_, okResp := a.postAdmission(t,
		admissionBody("uid-imm-upd-ok", "UPDATE", created, &created, false))
	if !okResp.Response.Allowed {
		t.Fatalf("unchanged update denied: %s", okResp.Response.Message)
	}

	// Changed CPU -> StateConflict.
	changed := created
	changed.Spec.CPU = "600m"
	_, bad := a.postAdmission(t,
		admissionBody("uid-imm-upd-bad", "UPDATE", changed, &created, false))
	if bad.Response.FailureCategory != types.CatStateConflict {
		t.Fatalf("expected StateConflict, got %s: %s",
			bad.Response.FailureCategory, bad.Response.Message)
	}
}

// E2E 8 — bad HTTP requests are rejected at the transport boundary with 400.
func TestE2ETransportValidation(t *testing.T) {
	a := bootApp(t, standardConfig)
	for _, body := range []map[string]any{
		{"request": map[string]any{"uid": "", "operation": "CREATE", "object": workload("w", "x")}},
		{"request": map[string]any{"uid": "u", "operation": "PATCH", "object": workload("w", "x")}},
	} {
		b := body
		if _, ok := b["apiVersion"]; !ok {
			b["apiVersion"] = "admission.example.com/v1"
			b["kind"] = "AdmissionReview"
		}
		raw, _ := jsonBytes(b)
		resp, err := a.client.Post(a.baseURL+"/admission", "application/json", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != 400 {
			t.Fatalf("expected 400, got %d for %v", resp.StatusCode, b)
		}
	}
}

// injectPlugin inserts a mutator fixture at the end of the mutators array.
func injectPlugin(cfg, pluginJSON string) string {
	marker := `    ],
    "validators"`
	idx := strings.Index(cfg, marker)
	if idx < 0 {
		panic("marker not found")
	}
	return cfg[:idx] + "      ," + pluginJSON + "\n    ],\n    \"validators\"" + cfg[idx+len(marker):]
}
