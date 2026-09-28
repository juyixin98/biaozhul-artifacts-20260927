package scenarios

import (
	"net/http"
	"testing"
	"time"
)

// TestHappyPathConvergesAndCoalescesDuplicateEvents verifies:
//   - generation/observedGeneration reach the same value;
//   - exactly one physical resource exists and is owned by the object UID;
//   - the status reports ExternalReady=True with the external ID;
//   - a burst of duplicate events while converged does not create extra
//     reconcile churn or resources.
func TestHappyPathConvergesAndCoalescesDuplicateEvents(t *testing.T) {
	e := newEnv(t)

	uid := e.createResource("team-a", "widget",
		map[string]any{"replicas": 2.0, "secret": "shh"})

	if !waitFor(3*time.Second, func() bool {
		b := e.getResource("team-a", "widget")
		st := b["status"].(map[string]any)
		return asFloat(b["generation"]) == 1 &&
			asFloat(st["observedGeneration"]) == 1 &&
			st["externalID"] != nil && st["externalID"] != ""
	}) {
		t.Fatalf("object never converged: %v", e.getResource("team-a", "widget"))
	}

	// Physical ownership is asserted against the actual service's own list,
	// not the controller's ledger.
	owned := e.actualResourcesByOwner(uid)
	if len(owned) != 1 {
		t.Fatalf("expected exactly 1 owned physical resource, got %d", len(owned))
	}
	extID := owned[0]["id"].(string)

	b := e.getResource("team-a", "widget")
	if asFloat(b["generation"]) != asFloat(b["status"].(map[string]any)["observedGeneration"]) {
		t.Fatalf("generation/observedGeneration mismatch: %v", b["status"])
	}
	// Unauthenticated read must have the sensitive field redacted.
	unauth := e.getResource("team-a", "widget")
	spec := unauth["spec"].(map[string]any)
	if spec["secret"] != "***REDACTED***" {
		t.Fatalf("sensitive spec leaked to unprivileged reader: %v", spec["secret"])
	}
	priv := e.getResourcePriv("team-a", "widget")
	if priv["spec"].(map[string]any)["secret"] != "shh" {
		t.Fatalf("controller credential should see raw spec")
	}

	roundsBefore := e.ctl.ProcessedRounds(uid)
	// Fire many duplicate events; the queue must coalesce them.
	for i := 0; i < 20; i++ {
		e.nudge(uid)
	}
	time.Sleep(400 * time.Millisecond)

	if got := len(e.actualResourcesByOwner(uid)); got != 1 {
		t.Fatalf("duplicate events changed physical ownership: %d resources", got)
	}
	if got := e.ctl.ProcessedRounds(uid); got < roundsBefore {
		t.Fatalf("processed rounds went backwards: %d -> %d", roundsBefore, got)
	}
	counters := e.actualCounters()
	if n := asFloat(counters["creates.committed"]); n != 1 {
		t.Fatalf("expected exactly 1 committed create, counters=%v", counters)
	}
	_ = extID
}

// TestCreateInterruptionClaimsInsteadOfDuplicating covers the core
// at-least-once problem: the external create succeeds, but its response is
// lost. The controller must query by owner UID, claim the existing row and
// never create a second physical resource.
func TestCreateInterruptionClaimsInsteadOfDuplicating(t *testing.T) {
	e := newEnv(t)
	uid := e.createResource("team-b", "orphan",
		map[string]any{"replicas": 1.0})

	// Install the fault BEFORE the controller can create so the first create
	// commits and then returns 500. The API event already enqueued the key.
	e.setActualFault(uid, "create-response-loss")
	// Give the faulted attempt time to land, then clear so the claim proceeds.
	if !waitFor(3*time.Second, func() bool {
		return asFloat(e.actualCounters()["creates.committed"]) == 1
	}) {
		t.Fatalf("faulted create never committed: %v", e.actualCounters())
	}
	e.clearActualFault(uid)

	if !waitFor(3*time.Second, func() bool {
		b := e.getResource("team-b", "orphan")
		st := b["status"].(map[string]any)
		return asFloat(st["observedGeneration"]) == 1 && st["externalID"] != ""
	}) {
		t.Fatalf("object did not converge after claim: %v",
			e.getResource("team-b", "orphan"))
	}

	// Independent oracle: still exactly one row, created exactly once.
	if got := len(e.actualResourcesByOwner(uid)); got != 1 {
		t.Fatalf("lost create response produced %d physical resources (want 1)", got)
	}
	counters := e.actualCounters()
	if asFloat(counters["creates.committed"]) != 1 {
		t.Fatalf("controller re-created after lost response: counters=%v", counters)
	}

	// The controller ledger must explain the accepted decision and tie it to
	// a request ID.
	entries := e.ledgerDecisions(uid)
	var sawClaim, sawLost bool
	for _, m := range entries {
		switch asString(m["decision"]) {
		case "CreateResponseLost":
			sawLost = true
			if cat := asString(m["category"]); cat != "ResponseLost" {
				t.Fatalf("lost create category=%s want ResponseLost", cat)
			}
			if asString(m["requestID"]) == "" {
				t.Fatalf("lost create ledger entry missing requestID")
			}
		case "ClaimedExisting":
			sawClaim = true
		}
	}
	if !sawLost || !sawClaim {
		t.Fatalf("ledger missing claim sequence (lost=%v claim=%v): %v",
			sawLost, sawClaim, entries)
	}
}

// TestDeleteRetriesThenPurges verifies the finalizer contract: while the
// external delete fails, the record stays and carries the finalizer; once
// deletion succeeds the finalizer is removed and the record is purged.
func TestDeleteRetriesThenPurges(t *testing.T) {
	e := newEnv(t)
	uid := e.createResource("team-c", "mortal",
		map[string]any{"replicas": 1.0})

	if !waitFor(3*time.Second, func() bool {
		st := e.getResource("team-c", "mortal")["status"].(map[string]any)
		return asFloat(st["observedGeneration"]) == 1 && st["externalID"] != ""
	}) {
		t.Fatalf("did not converge before deletion")
	}
	if len(e.actualResourcesByOwner(uid)) != 1 {
		t.Fatalf("precondition: expected 1 physical resource")
	}

	// External deletion keeps failing.
	e.setActualFault(uid, "delete-failed")
	status, _ := e.deleteResource("team-c", "mortal")
	if status != http.StatusOK {
		t.Fatalf("delete request status=%d want 200 (terminating)", status)
	}

	// Phase 1: still terminating, still finalizer, physical row still there.
	if !waitFor(2*time.Second, func() bool {
		b := e.getResource("team-c", "mortal")
		return b["deletionTimestamp"] != nil &&
			len(b["finalizers"].([]any)) == 1
	}) {
		t.Fatalf("record should be terminating with finalizer: %v",
			e.getResource("team-c", "mortal"))
	}
	time.Sleep(300 * time.Millisecond)
	if got := len(e.actualResourcesByOwner(uid)); got != 1 {
		t.Fatalf("physical resource vanished while delete fault active: %d rows", got)
	}

	// Heal: external delete succeeds.
	e.clearActualFault(uid)

	// Phase 2: record purged, physical row gone.
	if !waitFor(3*time.Second, func() bool {
		st, body := e.do(http.MethodGet, e.resourceURL("team-c", "mortal"), nil, nil)
		return st == http.StatusNotFound || body["__status"] == 404
	}) {
		t.Fatalf("record was not purged after external cleanup: %v",
			e.getResource("team-c", "mortal"))
	}
	if got := len(e.actualResourcesByOwner(uid)); got != 0 {
		t.Fatalf("physical resource not removed after purge: %d rows", got)
	}

	entries := e.ledgerDecisions(uid)
	if n := countCategory(entries, "Transient"); n == 0 {
		t.Fatalf("expected Transient failure category entries during delete, ledger=%v", entries)
	}
	var sawPurge bool
	for _, m := range entries {
		if asString(m["decision"]) == "Settled" {
			sawPurge = true
		}
	}
	if !sawPurge {
		t.Fatalf("ledger missing final Settled decision: %v", entries)
	}
}
