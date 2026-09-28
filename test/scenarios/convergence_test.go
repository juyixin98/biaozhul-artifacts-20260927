package scenarios

import (
	"net/http"
	"testing"
	"time"
)

// specOf extracts the physical spec from an actual-plane resource row.
func specOf(m map[string]any) map[string]any {
	s, _ := m["spec"].(map[string]any)
	return s
}

func replicas(m map[string]any) float64 {
	return asFloat(m["replicas"])
}

func (e *env) waitConverged(ns, name string, gen int64) map[string]any {
	e.t.Helper()
	if !waitFor(4*time.Second, func() bool {
		b := e.getResource(ns, name)
		st := b["status"].(map[string]any)
		return asFloat(b["generation"]) == float64(gen) &&
			asFloat(st["observedGeneration"]) == float64(gen) &&
			st["externalID"] != nil && st["externalID"] != ""
	}) {
		e.t.Fatalf("%s/%s never converged to generation %d: %v",
			ns, name, gen, e.getResource(ns, name))
	}
	return e.getResource(ns, name)
}

// TestGenerationAdvancesDrivesExternalUpdate verifies a real spec change
// bumps generation and the physical resource's spec follows; the observed
// generation never jumps ahead.
func TestGenerationAdvancesDrivesExternalUpdate(t *testing.T) {
	e := newEnv(t)
	uid := e.createResource("team-d", "roll", map[string]any{"replicas": 1.0})
	b := e.waitConverged("team-d", "roll", 1)
	rv := int64(asFloat(b["resourceVersion"]))

	status, body := e.updateSpec("team-d", "roll", rv,
		map[string]any{"replicas": 4.0})
	if status != http.StatusOK {
		t.Fatalf("spec update: %d %v", status, body)
	}
	e.waitConverged("team-d", "roll", 2)

	owned := e.actualResourcesByOwner(uid)
	if len(owned) != 1 {
		t.Fatalf("expected 1 physical resource after update, got %d", len(owned))
	}
	if got := replicas(specOf(owned[0])); got != 4 {
		t.Fatalf("physical replicas=%v want 4 (stale spec retained)", got)
	}
	if asFloat(owned[0]["generation"]) != 2 {
		t.Fatalf("physical generation=%v want 2", owned[0]["generation"])
	}
	if asFloat(owned[0]["version"]) < 2 {
		t.Fatalf("physical version should advance on update: %v", owned[0]["version"])
	}
}

// TestStaleObservationIsRejectedThenConverges covers a GET that returns an
// older snapshot than the version the controller has already acted on. The
// controller must classify it StaleObservation, refuse to regress, and
// converge once a fresh observation is served.
func TestStaleObservationIsRejectedThenConverges(t *testing.T) {
	e := newEnv(t)
	uid := e.createResource("team-e", "lagger", map[string]any{"replicas": 1.0})
	b := e.waitConverged("team-e", "lagger", 1)
	rv := int64(asFloat(b["resourceVersion"]))

	// Change spec; the update commits (and snapshots v1), then subsequent GETs
	// serve the stale v1 snapshot.
	stUp, upBody := e.updateSpec("team-e", "lagger", rv,
		map[string]any{"replicas": 5.0})
	if stUp != http.StatusOK {
		t.Fatalf("spec update: %d %v", stUp, upBody)
	}
	e.setActualFault(uid, "stale-get")

	// Wait until the ledger shows the stale decision with the right category.
	if !waitFor(4*time.Second, func() bool {
		return countCategory(e.ledgerDecisions(uid), "StaleObservation") > 0
	}) {
		t.Fatalf("controller never classified stale observation: %v",
			e.ledgerDecisions(uid))
	}

	// While stale, observedGeneration must remain 1 (never regress and never
	// falsely confirm gen 2).
	cur := e.getResource("team-e", "lagger")
	st := cur["status"].(map[string]any)
	if asFloat(st["observedGeneration"]) != 1 {
		t.Fatalf("observedGeneration moved while only stale reads available: %v",
			st["observedGeneration"])
	}
	// Physical truth is already v2/gen2 — the stale reads merely hid it.
	owned := e.actualResourcesByOwner(uid)
	if len(owned) != 1 || replicas(specOf(owned[0])) != 5 {
		t.Fatalf("physical update not committed before stale reads: %v", owned)
	}

	e.clearActualFault(uid)
	e.waitConverged("team-e", "lagger", 2)
}

// TestDesiredPlaneStatusConflictRequeuesWithoutOverwrite simulates a spec
// change arriving while the controller is about to write status for the old
// generation. The status write loses optimistic concurrency; the new spec
// must be preserved and the controller must requeue and converge to the new
// generation instead of overwriting it.
func TestDesiredPlaneStatusConflictRequeuesWithoutOverwrite(t *testing.T) {
	e := newEnv(t)
	uid := e.createResource("team-f", "racer", map[string]any{"replicas": 1.0})
	b := e.waitConverged("team-f", "racer", 1)
	rv := int64(asFloat(b["resourceVersion"]))

	// Arm a one-shot 409 on the next status write, then change the spec twice
	// rapidly so a status writer can race the newer generation.
	e.setDesiredFault(uid, "status-conflict-once")

	// Spec v2.
	st2, body2 := e.updateSpec("team-f", "racer", rv,
		map[string]any{"replicas": 2.0})
	if st2 != http.StatusOK {
		t.Fatalf("update to gen2: %d %v", st2, body2)
	}
	rv2 := int64(asFloat(body2["resourceVersion"]))
	// Spec v3 while controller may still be finishing gen2's status.
	st3, body3 := e.updateSpec("team-f", "racer", rv2,
		map[string]any{"replicas": 3.0})
	if st3 != http.StatusOK {
		t.Fatalf("update to gen3: %d %v", st3, body3)
	}

	e.waitConverged("team-f", "racer", 3)

	entries := e.ledgerDecisions(uid)
	if n := countDecision(entries, "StatusWriteConflict"); n == 0 {
		t.Fatalf("expected at least one StatusWriteConflict decision: %v", entries)
	}
	owned := e.actualResourcesByOwner(uid)
	if len(owned) != 1 {
		t.Fatalf("conflict churn created %d physical resources", len(owned))
	}
	if got := replicas(specOf(owned[0])); got != 3 {
		t.Fatalf("controller overwrote new spec: physical replicas=%v want 3", got)
	}
	counters := e.actualCounters()
	if asFloat(counters["creates.committed"]) != 1 {
		t.Fatalf("status conflict caused a recreate: %v", counters)
	}
}

// TestExternalUpdateConflictRequeues forces a conditional PUT to fail with
// 412. The controller must record Conflict, re-observe, retry, and never
// overwrite with a stale expected version.
func TestExternalUpdateConflictRequeues(t *testing.T) {
	e := newEnv(t)
	uid := e.createResource("team-g", "bump", map[string]any{"replicas": 1.0})
	b := e.waitConverged("team-g", "bump", 1)
	rv := int64(asFloat(b["resourceVersion"]))

	e.setActualFault(uid, "update-conflict")
	stUp, bodyUp := e.updateSpec("team-g", "bump", rv,
		map[string]any{"replicas": 7.0})
	if stUp != http.StatusOK {
		t.Fatalf("spec update: %d %v", stUp, bodyUp)
	}

	if !waitFor(4*time.Second, func() bool {
		return countCategory(e.ledgerDecisions(uid), "Conflict") > 0
	}) {
		t.Fatalf("expected Conflict category from forced 412: %v", e.ledgerDecisions(uid))
	}
	// Physical spec must remain v1 while conflicts are injected.
	time.Sleep(300 * time.Millisecond)
	owned := e.actualResourcesByOwner(uid)
	if len(owned) != 1 || replicas(specOf(owned[0])) != 1 {
		t.Fatalf("conflict path mutated physical resource unexpectedly: %v", owned)
	}

	e.clearActualFault(uid)
	e.waitConverged("team-g", "bump", 2)
	owned = e.actualResourcesByOwner(uid)
	if replicas(specOf(owned[0])) != 7 {
		t.Fatalf("did not converge after conflict cleared: %v", specOf(owned[0]))
	}
}

// TestDeleteResponseLossIsIdempotent deletes the physical row but loses the
// response. The controller must treat the outcome as undecidable, confirm by
// GET, and finish the delete without recreating the resource.
func TestDeleteResponseLossIsIdempotent(t *testing.T) {
	e := newEnv(t)
	uid := e.createResource("team-h", "ghost", map[string]any{"replicas": 1.0})
	e.waitConverged("team-h", "ghost", 1)

	e.setActualFault(uid, "delete-response-loss")
	if st, _ := e.deleteResource("team-h", "ghost"); st != http.StatusOK {
		t.Fatalf("delete request status=%d", st)
	}

	if !waitFor(4*time.Second, func() bool {
		return countDecision(e.ledgerDecisions(uid), "DeleteResponseLost") > 0
	}) {
		t.Fatalf("missing DeleteResponseLost decision: %v", e.ledgerDecisions(uid))
	}
	e.clearActualFault(uid)

	// Final state: record purged, no physical resource, no recreate.
	if !waitFor(4*time.Second, func() bool {
		st, _ := e.do(http.MethodGet, e.resourceURL("team-h", "ghost"), nil, nil)
		return st == http.StatusNotFound
	}) {
		t.Fatalf("record not purged after lost delete response")
	}
	if got := len(e.actualResourcesByOwner(uid)); got != 0 {
		t.Fatalf("controller recreated after lost delete: %d resources", got)
	}
	counters := e.actualCounters()
	if asFloat(counters["creates.committed"]) != 1 {
		t.Fatalf("delete ambiguity triggered extra creates: %v", counters)
	}
}
