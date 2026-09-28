package model

import "time"

// FailureCategory classifies why a reconcile attempt did not complete. The
// controller records one per ledger entry. Tests assert on these categories,
// not on log text.
type FailureCategory string

const (
	// CatNone means the decision was a successful/accepted action.
	CatNone FailureCategory = ""
	// CatConflict means a write lost optimistic concurrency (409).
	CatConflict FailureCategory = "Conflict"
	// CatResponseLost means an external mutation may have happened but its
	// response was lost (500 after commit).
	CatResponseLost FailureCategory = "ResponseLost"
	// CatTransient means retryable backend failure (5xx, timeouts, 429).
	CatTransient FailureCategory = "Transient"
	// CatStaleObservation means the external observation was older than the
	// state the controller has already acted on.
	CatStaleObservation FailureCategory = "StaleObservation"
	// CatNotFound means the referenced external resource no longer exists.
	CatNotFound FailureCategory = "NotFound"
	// CatRefused means the desired plane refused the request (e.g. status
	// observedGeneration ahead of generation).
	CatRefused FailureCategory = "Refused"
)

// Decision reason codes. They answer "why accepted, rejected or
// undecidable" in machine readable form.
const (
	DecCreateRequested      = "CreateRequested"
	DecCreateResponseLost   = "CreateResponseLost"
	DecClaimed              = "ClaimedExisting"
	DecUpdateRequested      = "UpdateRequested"
	DecUpdateConflict       = "UpdateConflict"
	DecDeleteRequested      = "DeleteRequested"
	DecDeleteResponseLost   = "DeleteResponseLost"
	DecObservedInSync       = "ObservedInSync"
	DecObservedStale        = "ObservedStale"
	DecObservedAhead        = "ObservedAhead"
	DecExternalMissing      = "ExternalMissing"
	DecFinalizerAdded       = "FinalizerAdded"
	DecFinalizerRemoved     = "FinalizerRemoved"
	DeletingWhileFinalizers = "DeletingWhileFinalizers"
	DecStatusWritten        = "StatusWritten"
	DecStatusConflict       = "StatusWriteConflict"
	DecRequeuedBackoff      = "RequeuedWithBackoff"
	DecSettled              = "Settled"
	DecUnexpectedError      = "UnexpectedError"
)

// LedgerEntry is one controller decision record.
type LedgerEntry struct {
	Seq                int64           `json:"seq"`
	UID                string          `json:"uid"`
	Attempt            int64           `json:"attempt"`
	Phase              string          `json:"phase"`
	Decision           string          `json:"decision"`
	Category           FailureCategory `json:"category"`
	Detail             string          `json:"detail"`
	ExternalID         string          `json:"externalID,omitempty"`
	DesiredGeneration  int64           `json:"desiredGeneration"`
	ObservedGeneration int64           `json:"observedGeneration"`
	ResourceVersion    int64           `json:"resourceVersion"`
	ActualVersion      int64           `json:"actualVersion,omitempty"`
	RequestID          string          `json:"requestID,omitempty"`
	CreatedAt          time.Time       `json:"createdAt"`
}
