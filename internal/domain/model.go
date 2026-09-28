// Package domain contains the core resource model of the interruption-budget
// eviction coordinator. It has no dependencies on storage or transport, so the
// rules it expresses can be unit-tested in isolation.
package domain

import (
	"errors"
	"time"
)

// BudgetMode selects how a group's interruption budget is expressed.
type BudgetMode string

const (
	// MinAvailable: at least this many (or this percentage of) members must
	// remain available after any set of approved voluntary evictions.
	MinAvailable BudgetMode = "minAvailable"
	// MaxUnavailable: at most this many (or this percentage of) members may be
	// unavailable at any point in time.
	MaxUnavailable BudgetMode = "maxUnavailable"
)

// Group is the replicated resource the coordinator protects. A real analogue
// is a PDB guarding a workload; here it is a plain local struct.
type Group struct {
	Name           string
	Namespace      string
	Replicas       int32 // declared desired replica count
	BudgetMode     BudgetMode
	BudgetValue    int32 // absolute count
	BudgetPercent  int32 // 1..100, used when BudgetValue == 0
	SelectorLabels map[string]string
	// SelectorEpoch is incremented every time the selector changes.
	// Observations and approvals carry the epoch they were made under.
	SelectorEpoch int64
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

func (g Group) Key() string { return g.Namespace + "/" + g.Name }

// Validate enforces a well-formed group definition.
func (g Group) Validate() error {
	if g.Name == "" || g.Namespace == "" {
		return errors.New("group name and namespace are required")
	}
	if g.Replicas <= 0 {
		return errors.New("group replicas must be positive")
	}
	if g.BudgetMode != MinAvailable && g.BudgetMode != MaxUnavailable {
		return errors.New("budget mode must be minAvailable or maxUnavailable")
	}
	if g.BudgetValue < 0 || g.BudgetPercent < 0 || g.BudgetPercent > 100 {
		return errors.New("invalid budget value")
	}
	if g.BudgetValue == 0 && g.BudgetPercent == 0 {
		return errors.New("a budget of zero disruptions allowed must be expressed explicitly")
	}
	if g.SelectorLabels == nil || len(g.SelectorLabels) == 0 {
		return errors.New("selector labels are required")
	}
	return nil
}

// Instance is a member replica. Membership is derived at observation time by
// matching labels against the group's selector under a concrete epoch.
type Instance struct {
	ID        string
	Group     string
	Namespace string
	Labels    map[string]string
}

// Observation is a readiness fact reported by a local synthetic adapter
// (stand-in for a pod/informers). Ready=false means voluntarily-not-ready or
// unknown; a hard failure is recorded separately as a Failure.
type Observation struct {
	InstanceID string
	Ready      bool
	// Epoch is the selector epoch the observer believed in. Observations from
	// an older epoch are stale and cannot drive a decision.
	Epoch  int64
	At     time.Time
	Source string
}

// Failure is an *involuntary* disruption fact: the instance died on its own
// (node loss, crash). Failures are recorded, never "denied": a budget cannot
// stop a failure. It can only be reported.
type Failure struct {
	InstanceID string
	Reason     string
	Epoch      int64
	At         time.Time
	Source     string
}

// ApprovalState is the lifecycle of an eviction approval.
type ApprovalState string

const (
	ApprovalPending   ApprovalState = "pending" // budget reserved, eviction not yet reported complete
	ApprovalSucceeded ApprovalState = "succeeded"
	ApprovalFailed    ApprovalState = "failed"  // client reported the eviction could not run
	ApprovalExpired   ApprovalState = "expired" // deadline passed; reservation not yet reclaimed
	ApprovalStale     ApprovalState = "stale"   // selector epoch moved on; not yet reclaimed
)

// Reclaimable reports whether a terminal-but-unreclaimed state still holds a
// budget reservation that must be explicitly released.
func (s ApprovalState) Reclaimable() bool {
	return s == ApprovalExpired || s == ApprovalStale
}

// Approval is a granted voluntary eviction and its budget reservation.
type Approval struct {
	ID         string
	Group      string
	Namespace  string
	InstanceID string
	Epoch      int64 // selector epoch the approval was granted under
	State      ApprovalState
	// ReservedAt is when the budget slot was taken.
	ReservedAt time.Time
	ExpiresAt  time.Time
	// ReclaimedAt is set once an explicit confirmation released the slot.
	ReclaimedAt  *time.Time
	ResultReason string
}

// BudgetSnapshot is the reconciled, decision-relevant state of one group. It is
// embedded in every Decision so diagnostics show exactly which numbers drove
// accept/reject/cannot-decide.
type BudgetSnapshot struct {
	Group              string `json:"group"`
	SelectorEpoch      int64  `json:"selector_epoch"`
	DesiredReplicas    int32  `json:"desired_replicas"`
	ReadyReplicas      int32  `json:"ready_replicas"`
	CurrentUnavailable int32  `json:"current_unavailable"`
	// ChargedApprovals is the subset of current_unavailable caused by
	// reservations that are still held: approved-but-unfinished evictions plus
	// expired/stale approvals awaiting explicit reclamation.
	ChargedApprovals int32 `json:"charged_approvals"`
	// UnknownReplicas are members with no current-epoch readiness observation.
	// Any unknown member forces Fresh=false ("cannot decide").
	UnknownReplicas    int32 `json:"unknown_replicas"`
	FailedReplicas     int32 `json:"failed_replicas"`
	AllowedUnavailable int32 `json:"allowed_unavailable"`
	AvailableSlots     int32 `json:"available_slots"`
	// Fresh is false when observations are missing or from an older epoch.
	Fresh bool `json:"fresh"`
}

// Decision is the structured outcome of an eviction request.
type Decision struct {
	RequestID string `json:"request_id"`
	Group     string `json:"group"`
	Instance  string `json:"instance"`
	// Accepted is true only when a budget slot was atomically reserved.
	Accepted   bool            `json:"accepted"`
	Category   FailureCategory `json:"category,omitempty"`
	Reason     string          `json:"reason,omitempty"`
	ApprovalID string          `json:"approval_id,omitempty"`
	Snapshot   BudgetSnapshot  `json:"snapshot"`
}

// FailureCategory is the machine-readable class of a non-acceptance. The test
// suite asserts on these exact values rather than on human-readable strings.
type FailureCategory string

const (
	// CatNone: request accepted.
	CatNone FailureCategory = ""
	// CatGroupNotFound: no such group.
	CatGroupNotFound FailureCategory = "group_not_found"
	// CatInstanceNotMember: selector (current epoch) does not match.
	CatInstanceNotMember FailureCategory = "instance_not_member"
	// CatStaleEpoch: observation/request built against an old selector epoch.
	CatStaleEpoch FailureCategory = "stale_selector_epoch"
	// CatUnknownReadiness: no current-epoch readiness observation -> cannot decide.
	CatUnknownReadiness FailureCategory = "unknown_readiness"
	// CatInstanceFailed: instance has an involuntary failure; not a budget matter.
	CatInstanceFailed FailureCategory = "instance_failed"
	// CatAlreadyEvicting: a live approval for this instance already holds a slot.
	CatAlreadyEvicting FailureCategory = "already_evicting"
	// CatBudgetExhausted: real budget computation says no slot is available.
	CatBudgetExhausted FailureCategory = "budget_exhausted"
	// CatApprovalNotFound / CatApprovalState / CatReclaimConfirmation: lifecycle API.
	CatApprovalNotFound    FailureCategory = "approval_not_found"
	CatApprovalState       FailureCategory = "approval_state_conflict"
	CatReclaimConfirmation FailureCategory = "reclaim_confirmation_required"
)

// String keeps the zero value readable in logs.
func (c FailureCategory) String() string {
	if c == CatNone {
		return "accepted"
	}
	return string(c)
}
