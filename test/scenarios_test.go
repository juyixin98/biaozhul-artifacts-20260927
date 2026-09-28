package e2e

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"
)

// TestE2E_HappyPathAndDuplicateEvents: a fresh resource converges, and a
// burst of duplicate desired-state events converges to exactly one external
// object whose version never moves for identical specs.
func TestE2E_HappyPathAndDuplicateEvents(t *testing.T) {
	h := startHarness(t)
	const name = "dup-events"

	status, _ := h.createWidget(name, "blue", 2, "secret-alpha")
	if status != http.StatusCreated {
		t.Fatalf("create status = %d", status)
	}
	ready := h.waitReady(name, 1)

	// Independent oracle: the actual service holds exactly one object at the
	// requested spec, with the claimed id and version.
	fs, fr := h.fakeGet(ready.Status.ExternalID)
	if fs != http.StatusOK {
		t.Fatalf("fake GET: %d", fs)
	}
	if fr.Spec.Color != "blue" || fr.Spec.Replicas != 2 || fr.Version != 1 {
		t.Fatalf("actual resource mismatch: %+v", fr)
	}
	if h.fakeListCount() != 1 {
		t.Fatalf("actual service should own exactly one object, count=%d", h.fakeListCount())
	}

	// Duplicate create event for the same name is refused, not upserted.
	if st, _ := h.createWidget(name, "red", 9, "secret-beta"); st != http.StatusConflict {
		t.Fatalf("duplicate create: status=%d, want 409", st)
	}

	// Several identical-spec updates (same intent delivered repeatedly). The
	// generation advances each time, but the external object must not be
	// rewritten: it already matches, so its version stays 1.
	rv := ready.Metadata.ResourceVersion
	for i := 0; i < 3; i++ {
		st, v := h.putWidget(name, rv, "blue", 2)
		if st != http.StatusOK {
			t.Fatalf("identical-spec put %d: %d", i, st)
		}
		rv = v.Metadata.ResourceVersion
	}
	final := h.waitReady(name, 4)
	_, fr = h.fakeGet(final.Status.ExternalID)
	if fr.Version != 1 {
		t.Fatalf("identical-spec re-delivery rewrote external object: version=%d", fr.Version)
	}
	if c := strings.Count(h.logs.String(), `"msg":"fakecloud resource updated"`); c != 0 {
		t.Fatalf("expected 0 external updates for identical specs, saw %d", c)
	}
	if c := strings.Count(h.logs.String(), `"msg":"fakecloud resource created"`); c != 1 {
		t.Fatalf("expected exactly 1 external create in service log, saw %d", c)
	}
}

// TestE2E_CreateResponseLostClaimsNotRecreates: external create commits but
// the response is lost. The controller must claim by observation and must
// never create a second physical object.
func TestE2E_CreateResponseLostClaimsNotRecreates(t *testing.T) {
	h := startHarness(t)
	const name = "create-lost"

	h.armFault("create", "createResponseLost", 1)
	status, _ := h.createWidget(name, "green", 3, "secret-gamma")
	if status != http.StatusCreated {
		t.Fatalf("create status = %d", status)
	}

	// Convergence despite the ambiguous first attempt.
	ready := h.waitReady(name, 1)
	if ready.Status.LastAttempt == nil {
		t.Fatal("missing last attempt diagnostics")
	}

	// Independent ownership oracle: exactly one object in the real service.
	fs, fr := h.fakeGet(ready.Status.ExternalID)
	if fs != http.StatusOK {
		t.Fatalf("claimed object missing in actual service: %d", fs)
	}
	if fr.Spec.Color != "green" || fr.Spec.Replicas != 3 {
		t.Fatalf("claimed object spec mismatch: %+v", fr)
	}
	if h.fakeListCount() != 1 {
		t.Fatalf("response loss must not create duplicates, count=%d", h.fakeListCount())
	}
	// The actual service itself logs one committed create; a second create
	// attempt would show up here even though PUT is idempotent on replay.
	if c := strings.Count(h.logs.String(), `"msg":"fakecloud resource created"`); c != 1 {
		t.Fatalf("expected exactly 1 committed create, service log shows %d", c)
	}
}

// TestE2E_DeleteRetriesThenRecordRemovedOnlyAfterCleanup: a failed external
// delete is retried; mid-failure the record, finalizer and external object
// all remain. Only after the service confirms 404 does the record disappear.
func TestE2E_DeleteRetriesThenRecordRemovedOnlyAfterCleanup(t *testing.T) {
	h := startHarness(t)
	const name = "delete-retry"

	_, created := h.createWidget(name, "yellow", 1, "secret-delta")
	ready := h.waitReady(name, 1)
	extID := ready.Status.ExternalID

	h.armFault("delete", "deleteFail", 1)
	if st := h.deleteWidget(name); st != http.StatusAccepted {
		t.Fatalf("delete status = %d, want 202", st)
	}

	// Mid-deletion ownership: wait until the injected delete failure has
	// actually been consumed, with the external object still alive and the
	// finalizer/record still in place. (The API already reports phase
	// "Deleting" at request time, so phase alone is not sufficient evidence.)
	h.waitFor("delete failure observed while object and finalizer remain", 8*time.Second, func() bool {
		fs, fr := h.fakeGet(extID)
		_, w := h.getWidget(name)
		return fs == http.StatusOK && fr != nil && w != nil &&
			len(w.Metadata.Finalizers) == 1 &&
			strings.Contains(h.logs.String(), `"msg":"fakecloud delete injected failure"`)
	})
	if c := strings.Count(h.logs.String(), `"msg":"fakecloud delete injected failure"`); c != 1 {
		t.Fatalf("expected 1 injected delete failure, saw %d", c)
	}

	// Fault is single-shot and now consumed; retries converge.
	h.waitGone(name)
	if fs, _ := h.fakeGet(extID); fs != http.StatusNotFound {
		t.Fatalf("external object should be gone, status=%d", fs)
	}
	if c := strings.Count(h.logs.String(), `"msg":"fakecloud resource deleted"`); c != 1 {
		t.Fatalf("expected exactly 1 successful external delete, saw %d", c)
	}
	_ = created
}

// TestE2E_StaleObservationDoesNotCompleteGeneration: a read replica style
// stale GET after a gen-2 update must not be accepted as evidence; the
// completed generation stays at 1 until fresh reads confirm gen 2.
func TestE2E_StaleObservationDoesNotCompleteGeneration(t *testing.T) {
	h := startHarness(t)
	const name = "stale-obs"

	if st, _ := h.createWidget(name, "red", 1, "secret-eps"); st != http.StatusCreated {
		t.Fatalf("create: %d", st)
	}
	ready := h.waitReady(name, 1)
	extID := ready.Status.ExternalID

	// Armed before the update: while no update has happened there is no
	// snapshot, so ordinary reads are unaffected and the fault stays armed.
	h.armFault("get", "staleGet", 1)

	st, updated := h.putWidget(name, ready.Metadata.ResourceVersion, "purple", 7)
	if st != http.StatusOK {
		t.Fatalf("gen2 put: %d", st)
	}
	if updated.Metadata.Generation != 2 {
		t.Fatalf("desired generation = %d, want 2", updated.Metadata.Generation)
	}

	// Wait for the controller to refuse the stale confirm. The update is
	// applied at the service, but the read used to confirm it still reports
	// the pre-update observation; that ambiguity must not complete gen 2. The
	// decision is also recorded as an audit line that persists after the
	// requeue, so either the live status or the log proves the refusal.
	h.waitFor("stale confirmation rejected while desired gen=2", 8*time.Second, func() bool {
		_, w := h.getWidget(name)
		logs := h.logs.String()
		statusShowsRefusal := w != nil && w.Metadata.Generation == 2 &&
			w.Status.ReconciledGeneration == 1 && w.Status.LastAttempt != nil &&
			(w.Status.LastAttempt.Reason == "post-update-mismatch" ||
				w.Status.LastAttempt.Reason == "stale-observation")
		logShowsRefusal := strings.Contains(logs, `"reason":"post-update-mismatch"`) ||
			strings.Contains(logs, `"reason":"stale-observation"`)
		return statusShowsRefusal || logShowsRefusal
	})
	// The completed generation must not have advanced on the stale evidence
	// at any time before the next pass re-confirmed.
	if !strings.Contains(h.logs.String(), `"decision":"undecidable"`) {
		t.Fatal("expected an undecidable decision record for the stale confirmation")
	}
	_, mid := h.getWidget(name)
	if mid.Metadata.Generation != 2 {
		t.Fatalf("desired generation lost: %d", mid.Metadata.Generation)
	}

	final := h.waitReady(name, 2)
	fs, fr := h.fakeGet(extID)
	if fs != http.StatusOK {
		t.Fatalf("fake get after convergence: %d", fs)
	}
	if fr.Spec.Color != "purple" || fr.Spec.Replicas != 7 || fr.Version != 2 {
		t.Fatalf("actual resource not at gen2: %+v", fr)
	}
	if final.Status.ExternalVersion != 2 {
		t.Fatalf("controller externalVersion=%d, want 2", final.Status.ExternalVersion)
	}
}

// TestE2E_APISpecWriteCAS: a stale If-Match update is rejected with 409 and
// the newer spec is provably untouched; reloading and reusing the current
// version succeeds.
func TestE2E_APISpecWriteCAS(t *testing.T) {
	h := startHarness(t)
	const name = "cas-api"

	if st, _ := h.createWidget(name, "red", 1, "secret-zeta"); st != http.StatusCreated {
		t.Fatalf("create: %d", st)
	}
	ready := h.waitReady(name, 1)

	// A stale writer holds the original resource version while the resource
	// has since moved.
	if fwd, _ := h.putWidget(name, ready.Metadata.ResourceVersion, "aqua", 4); fwd != http.StatusOK {
		t.Fatalf("forward update: %d", fwd)
	}
	h.waitReady(name, 2)

	st, body := h.do(http.MethodPut, h.ctrlURL+"/api/v1/widgets/"+name, map[string]any{
		"spec": map[string]any{"replicas": 99, "color": "stale-color"},
	}, map[string]string{"If-Match": "1"})
	if st != http.StatusConflict {
		t.Fatalf("stale CAS: status=%d body=%s, want 409", st, body)
	}

	// The newer intent is intact; generation 3 never happened.
	_, w := h.getWidget(name)
	if w.Spec.Color != "aqua" || w.Spec.Replicas != 4 || w.Metadata.Generation != 2 {
		t.Fatalf("stale write changed state: %+v", w)
	}

	// Reload the current resource version (the reconcile loop's status
	// writes have advanced it since the spec write) and retry: accepted.
	_, current := h.getWidget(name)
	if st, _ = h.putWidget(name, current.Metadata.ResourceVersion, "teal", 5); st != http.StatusOK {
		t.Fatalf("reloaded CAS: status=%d, want 200", st)
	}
	final := h.waitReady(name, 3)
	fs, fr := h.fakeGet(final.Status.ExternalID)
	if fs != http.StatusOK || fr.Spec.Color != "teal" || fr.Spec.Replicas != 5 {
		t.Fatalf("gen3 not applied to actual service: fs=%d fr=%+v", fs, fr)
	}
}

// TestE2E_LogDiagnosticsCorrelatedAndRedacted checks the audit properties:
// request ids propagate, decisions carry identity and key state, and the
// secret is never present verbatim in any output.
func TestE2E_LogDiagnosticsCorrelatedAndRedacted(t *testing.T) {
	h := startHarness(t)
	const name = "diag"
	const secret = "supersecret-diag-value"
	const rid = "corr-id-1234567890"

	st, b := h.do(http.MethodPost, h.ctrlURL+"/api/v1/widgets", map[string]any{
		"metadata": map[string]string{"name": name},
		"spec":     map[string]any{"replicas": 1, "color": "grey", "secretToken": secret},
	}, map[string]string{"X-Request-Id": rid})
	if st != http.StatusCreated {
		t.Fatalf("create: %d %s", st, b)
	}
	h.waitReady(name, 1)

	// The API echoes the correlation id; its http log line carries it.
	if !strings.Contains(h.logs.String(), `"requestId":"`+rid+`"`) {
		t.Errorf("correlation id not found in logs")
	}
	// The raw secret must not appear in any log output (controller or the
	// actual service), while a redacted marker must.
	if strings.Contains(h.logs.String(), secret) {
		t.Errorf("raw secret leaked into logs:\n%s", h.logs.String())
	}
	if !strings.Contains(h.logs.String(), "su****e") {
		t.Errorf("expected redacted secret marker su****e in logs")
	}
	// Decision records name, generation state and an accept decision.
	if !strings.Contains(h.logs.String(), `"resource":"diag"`) {
		t.Errorf("decision log missing resource identity")
	}
	if !strings.Contains(h.logs.String(), `"reconciledGen":1`) {
		t.Errorf("decision log missing completed generation state")
	}

	// The API never echoes the secret itself.
	_, body := h.getWidget(name)
	raw, _ := json.Marshal(body)
	if strings.Contains(string(raw), secret) {
		t.Errorf("API response leaked secret: %s", raw)
	}
}
