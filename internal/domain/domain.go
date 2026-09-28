// Package domain holds the resource model and the pure budget/decision rules
// for voluntary eviction coordination in a local replica group.
//
// Nothing in this package performs I/O: every calculation is a deterministic
// function of the readiness observation it is handed. That makes the rules
// independently testable and lets the independent oracle (test/oracle) mirror
// them without depending on the implementation under test.
package domain

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

// InstanceState is the lifecycle state of a replica instance as observed on
// the cluster. Involuntary states are terminal and can never be undone by the
// coordinator.
type InstanceState string

const (
	StateReady    InstanceState = "ready"    // up, serving, eligible to be voluntarily evicted
	StateDraining InstanceState = "draining" // a voluntary eviction is in progress
	StateGone     InstanceState = "gone"     // voluntarily evicted; the move completed
	StateFailed   InstanceState = "failed"   // involuntary loss (crash, host death, ...)
)

func (s InstanceState) Valid() bool {
	switch s {
	case StateReady, StateDraining, StateGone, StateFailed:
		return true
	}
	return false
}

// Serving reports whether the instance currently contributes to the group's
// available serving capacity.
func (s InstanceState) Serving() bool { return s == StateReady || s == StateDraining }

// Involuntary reports whether the state is an involuntary failure that the
// interrupt budget can never prevent or "charge" to a voluntary eviction.
func (s InstanceState) Involuntary() bool { return s == StateFailed }

// EvictionPhase is the state machine of a single voluntary eviction request.
type EvictionPhase string

const (
	PhaseApproved  EvictionPhase = "approved"  // budget reserved; drain in progress
	PhaseCompleted EvictionPhase = "completed" // instance observed gone voluntarily
	PhaseFailed    EvictionPhase = "failed"    // instance failed before completion (no budget charge)
	PhaseExpired   EvictionPhase = "expired"   // approval TTL elapsed without a completion
	PhaseRevoked   EvictionPhase = "revoked"   // invalidated by a selector version change
)

// Terminal phases never consume a budget reservation anymore.
func (p EvictionPhase) Terminal() bool {
	return p != PhaseApproved
}

// Decision reason codes. Accepted range is "accepted:*"; everything else is a
// concrete, testable failure category instead of a generic "no".
const (
	ReasonAccepted                = "accepted:budget-available"
	ReasonUnknownInstance         = "rejected:unknown-instance"
	ReasonSelectorMismatch        = "rejected:selector-mismatch"
	ReasonNotReady                = "rejected:not-ready"
	ReasonInvoluntaryFailure      = "rejected:involuntary-failure"
	ReasonDuplicateEviction       = "rejected:duplicate-eviction"
	ReasonBudgetExhausted         = "rejected:budget-exhausted"
	ReasonStaleObservation        = "undecidable:stale-observation"
	ReasonEvictionNotFound        = "rejected:eviction-not-found"
	ReasonEvictionAlreadyTerminal = "rejected:eviction-already-terminal"
	ReasonEvictionStillOpen       = "rejected:eviction-still-open"
)

// ErrNotFound is returned by adapters when a referenced row does not exist.
var ErrNotFound = errors.New("domain: not found")

// Selector identifies the set of replicas an eviction may target. A selector
// is immutable once published; changing the labels (pod-template change,
// zone relocation, ...) publishes a NEW version. Coordination decisions are
// always bound to a concrete version so an approval issued against an old
// selection can never silently apply to the new one.
type Selector struct {
	Group        string    `json:"group"`
	Version      int64     `json:"version"`
	MatchLabels  string    `json:"match_labels"` // canonical "k=v,k=v" label expression
	EffectiveAt  time.Time `json:"effective_at"`
}

// Matches reports whether an instance carrying instanceLabels is selected.
// Labels are canonicalized to "k=v" pairs sorted by key; matching is an
// exact superset check: the instance must carry every selector label.
func (s Selector) Matches(instanceLabels map[string]string) bool {
	for k, v := range parseLabels(s.MatchLabels) {
		if instanceLabels[k] != v {
			return false
		}
	}
	return true
}

// CanonicalLabels renders a label map as the canonical selector expression.
func CanonicalLabels(m map[string]string) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	// simple insertion sort; label sets are tiny.
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0 && keys[j-1] > keys[j]; j-- {
			keys[j-1], keys[j] = keys[j], keys[j-1]
		}
	}
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+m[k])
	}
	return strings.Join(parts, ",")
}

func parseLabels(expr string) map[string]string {
	return ParseLabels(expr)
}

// ParseLabels parses a canonical "k=v,k=v" label expression into a map.
func ParseLabels(expr string) map[string]string {
	out := map[string]string{}
	expr = strings.TrimSpace(expr)
	if expr == "" {
		return out
	}
	for _, pair := range strings.Split(expr, ",") {
		kv := strings.SplitN(strings.TrimSpace(pair), "=", 2)
		if len(kv) == 2 {
			out[strings.TrimSpace(kv[0])] = strings.TrimSpace(kv[1])
		}
	}
	return out
}

// Instance is the coordinator's view of one replica.
type Instance struct {
	ID         string            `json:"id"`
	Group      string            `json:"group"`
	Labels     map[string]string `json:"labels"`
	State      InstanceState     `json:"state"`
	SelVersion int64             `json:"selector_version"` // version the instance currently belongs to
}

// Observation is a freshness-bounded readiness sample for a whole group.
// Budget math is only valid while the observation is fresh; an expired sample
// yields "undecidable", never an accept or a reject pretending to be current.
type Observation struct {
	Group     string
	Instances []Instance
	At        time.Time
	MaxAge    time.Duration
}

// Fresh reports whether the observation may be used for a decision at now.
func (o Observation) Fresh(now time.Time) bool {
	if o.MaxAge <= 0 {
		return true
	}
	return now.Sub(o.At) <= o.MaxAge
}

// Policy configures the interrupt budget for one group. Exactly one budget
// mode is active:
//
//   - MinAvailable > 0: at least MinAvailable replicas must stay serving
//     (equivalently at most Replicas-MinAvailable may be unavailable).
//   - MaxUnavailable > 0: at most MaxUnavailable replicas may be unavailable.
//
// Unavailable count is taken from REAL observations: replicas observed
// failed, gone (completed voluntary moves) or draining (approved moves in
// flight), plus approved-but-not-yet-observed reservations.
type Policy struct {
	Group          string `json:"group"`
	MinAvailable   int    `json:"min_available"`
	MaxUnavailable int    `json:"max_unavailable"`
	// ApproveTTL bounds how long an approval reserves budget without progress.
	ApproveTTL time.Duration `json:"-"`
	// CompletionTimeout bounds how long a draining instance may stay draining
	// before the drain is treated as stalled (still reported as undecidable,
	// not as an involuntary failure).
	CompletionTimeout time.Duration `json:"-"`
}

func (p Policy) validate(n int) error {
	if p.MinAvailable < 0 || p.MaxUnavailable < 0 {
		return errors.New("domain: budget values must be non-negative")
	}
	if p.MinAvailable == 0 && p.MaxUnavailable == 0 {
		return errors.New("domain: one of min_available/max_unavailable must be > 0")
	}
	if p.MinAvailable > 0 && p.MaxUnavailable > 0 {
		return errors.New("domain: min_available and max_unavailable are mutually exclusive")
	}
	if p.MinAvailable > n {
		return fmt.Errorf("domain: min_available %d exceeds group size %d", p.MinAvailable, n)
	}
	return nil
}

// Budget is the evaluated interrupt budget for a group at one point in time.
type Budget struct {
	Group          string `json:"group"`
	Replicas       int    `json:"replicas"`
	MinAvailable   int    `json:"min_available"`
	MaxUnavailable int    `json:"max_unavailable"`
	// Real observed unavailable, split by cause so diagnostics can show it.
	ObservedFailed   int `json:"observed_failed"`
	ObservedGone     int `json:"observed_gone"`
	ObservedDraining int `json:"observed_draining"`
	// Approved reservations not yet reflected in the observation (approved
	// evictions whose target still shows ready). These MUST be counted so a
	// burst of approvals before the next observation tick cannot over-admit.
	ApprovedPending int `json:"approved_pending"`
	// Allowance is the total unavailable slots the policy permits.
	Allowance int `json:"allowance"`
}

// ObservedUnavailable is everything the real observation already shows as
// not serving. Draining counts as unavailable: capacity is interrupted the
// moment a voluntary move starts, not only when it finishes.
func (b Budget) ObservedUnavailable() int {
	return b.ObservedFailed + b.ObservedGone + b.ObservedDraining
}

// Reserved is the budget currently spoken for (observation + reservations).
// A draining observation and its own approval are the SAME interruption and
// are therefore counted once: an approved eviction is only added in
// ApprovedPending when its target is not already observed draining.
func (b Budget) Reserved() int { return b.ObservedUnavailable() + b.ApprovedPending }

// Remaining is how many further voluntary evictions may be approved now.
// Involuntary failures already occupy unavailable slots, so a group that has
// lost replicas involuntarily has proportionally less voluntary headroom —
// but the failures themselves are never reported as budget-preventable.
func (b Budget) Remaining() int {
	r := b.Allowance - b.Reserved()
	if r < 0 {
		return 0
	}
	return r
}

// OverBudget reports whether reservations exceed the allowance. This can be
// true transiently when an involuntary failure pushes a group over its
// interrupt budget; the coordinator surfaces it as a failure, not as
// something a future voluntary request is to blame for.
func (b Budget) OverBudget() bool { return b.Reserved() > b.Allowance }

// ApprovedEviction is the minimal projection the budget math needs from the
// eviction table: an open approval and the instance it reserves.
type ApprovedEviction struct {
	ID         string
	InstanceID string
}

// EvaluateBudget computes the budget from a real readiness observation and
// the set of currently approved (non-terminal) evictions. It is deliberately
// side-effect free and takes every input explicitly so both the coordinator
// and the independent test oracle can call the identical rule.
func EvaluateBudget(obs Observation, pol Policy, approved []ApprovedEviction) (Budget, error) {
	if err := pol.validate(len(obs.Instances)); err != nil {
		return Budget{}, err
	}
	b := Budget{
		Group:          obs.Group,
		Replicas:       len(obs.Instances),
		MinAvailable:   pol.MinAvailable,
		MaxUnavailable: pol.MaxUnavailable,
	}
	draining := map[string]bool{}
	for _, in := range obs.Instances {
		switch in.State {
		case StateFailed:
			b.ObservedFailed++
		case StateGone:
			b.ObservedGone++
		case StateDraining:
			b.ObservedDraining++
			draining[in.ID] = true
		}
	}
	for _, a := range approved {
		// Count only approvals the observation does not already reflect:
		//   draining target -> the draining slot above already counts it
		//   ready target     -> hidden reservation, MUST count (prevents
		//                        over-admitting in the observation lag window)
		//   gone/failed      -> dead record the reaper clears; never holds budget
		//   unknown target    -> keep the reservation until explicitly reaped,
		//                        never silently free budget
		if !draining[a.InstanceID] && reservationLive(obs, a.InstanceID) {
			b.ApprovedPending++
		}
	}
	if pol.MaxUnavailable > 0 {
		b.Allowance = pol.MaxUnavailable
	} else {
		b.Allowance = b.Replicas - pol.MinAvailable
	}
	return b, nil
}

// reservationLive reports whether an open approval for instanceID still holds
// a hidden budget reservation under the given observation.
func reservationLive(obs Observation, instanceID string) bool {
	for _, in := range obs.Instances {
		if in.ID == instanceID {
			return in.State == StateReady
		}
	}
	return true // vanished from observation: keep until explicitly reaped
}

// Request captures one admission decision input.
type Request struct {
	InstanceID  string
	SelVersion  int64 // selector version the client pinned when asking
	Now         time.Time
}

// Decision is the result of admitting a single voluntary eviction.
type Decision struct {
	Accepted bool   `json:"accepted"`
	Reason   string `json:"reason"`
	Detail   string `json:"detail"`
	Budget   Budget `json:"budget"`
}

// Admit decides whether one more voluntary eviction may be approved against
// an evaluated budget. It does NOT mutate state; persistence/atomicity is the
// store's job (see store.TxApprove). Keeping the rule pure is what allows the
// oracle to re-derive expected answers independently.
func Admit(req Request, obs Observation, sel Selector, b Budget) Decision {
	var inst *Instance
	for i := range obs.Instances {
		if obs.Instances[i].ID == req.InstanceID {
			inst = &obs.Instances[i]
			break
		}
	}
	if inst == nil {
		return Decision{Reason: ReasonUnknownInstance,
			Detail: "instance is not part of the latest group observation", Budget: b}
	}
	if inst.Group != sel.Group || sel.Version != req.SelVersion || inst.SelVersion != sel.Version {
		return Decision{Reason: ReasonSelectorMismatch,
			Detail: fmt.Sprintf("request pinned selector v%d, instance belongs to v%d, current v%d",
				req.SelVersion, inst.SelVersion, sel.Version), Budget: b}
	}
	if !sel.Matches(inst.Labels) {
		return Decision{Reason: ReasonSelectorMismatch,
			Detail: "instance labels no longer match the current selector expression", Budget: b}
	}
	switch inst.State {
	case StateFailed:
		// Involuntary loss is not something an interrupt budget can block or
		// permit: there is no voluntary action left to approve.
		return Decision{Reason: ReasonInvoluntaryFailure,
			Detail: "instance already failed involuntarily; no voluntary eviction is possible", Budget: b}
	case StateGone:
		return Decision{Reason: ReasonNotReady,
			Detail: "instance is already gone", Budget: b}
	case StateDraining:
		return Decision{Reason: ReasonDuplicateEviction,
			Detail: "instance is already draining under an approved eviction", Budget: b}
	}
	if b.Remaining() <= 0 {
		return Decision{Reason: ReasonBudgetExhausted,
			Detail: fmt.Sprintf("allowance %d fully reserved: failed=%d gone=%d draining=%d approved_pending=%d",
				b.Allowance, b.ObservedFailed, b.ObservedGone, b.ObservedDraining, b.ApprovedPending),
			Budget: b}
	}
	return Decision{Accepted: true, Reason: ReasonAccepted,
		Detail: fmt.Sprintf("1 of %d budget slots free", b.Remaining()), Budget: b}
}
