package model

import "time"

// GCEvent is one append-only audit record emitted by the reconciler.
// Every deletion decision (block, cascade, orphan-strip, cycle break,
// finalizer outcome, physical delete) produces an event so that tests
// and operators can see the progress and the decision basis, instead of
// only observing the final set of rows.
type GCEvent struct {
	ID         int64     `json:"id"`
	RunID      string    `json:"runId"`
	Tick       int64     `json:"tick"`
	Step       int       `json:"step"` // ordering inside a tick
	Type       string    `json:"type"`
	Namespace  string    `json:"namespace,omitempty"`
	Name       string    `json:"name,omitempty"`
	UID        string    `json:"uid,omitempty"`
	OtherUID   string    `json:"otherUid,omitempty"`
	OtherName  string    `json:"otherName,omitempty"`
	Policy     string    `json:"policy,omitempty"`
	Finalizer  string    `json:"finalizer,omitempty"`
	Reason     string    `json:"reason,omitempty"`
	Message    string    `json:"message,omitempty"`
	OccurredAt time.Time `json:"occurredAt"`
}

// GC event types.
const (
	// EvFinalizerCalled / EvFinalizerFailed / EvFinalizerRecovered
	// record each user-finalizer attempt and its concrete outcome.
	EvFinalizerCalled   = "FinalizerCalled"
	EvFinalizerFailed   = "FinalizerFailed"
	EvFinalizerRecovered = "FinalizerRecovered"
	EvUnknownFinalizer  = "UnknownFinalizer"

	// EvOrphanRefStripped: one ownerReference pointing at an
	// Orphan-deleted owner was removed from a dependent.
	EvOrphanRefStripped = "OrphanRefStripped"

	// EvStaleRefDropped: a reference could not resolve against a live
	// owner (missing, or same name but different UID) and was dropped
	// during cascade evaluation.
	EvStaleRefDropped = "StaleRefDropped"

	// EvDeletionMarked: a cascade decision marked a live resource for
	// deletion (with the inherited policy).
	EvDeletionMarked = "DeletionMarked"

	// EvDeletionBlocked: foreground deletion waits for a blocking
	// dependent that still exists.
	EvDeletionBlocked = "DeletionBlocked"

	// EvCycleDetected / EvCycleBroken: an illegal cycle of foreground
	// deletion blocking edges was diagnosed and deterministically broken
	// (never waited on forever).
	EvCycleDetected = "CycleDetected"
	EvCycleBroken   = "CycleBroken"

	// EvResourceDeleted: the physical delete happened. Policy is the
	// policy under which the resource was actually deleted.
	EvResourceDeleted = "ResourceDeleted"

	// EvInvariantBroken: an internal assertion fired; the tick aborts
	// and the condition is reported as an error rather than success.
	EvInvariantBroken = "InvariantBroken"
)
