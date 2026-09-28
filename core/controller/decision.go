package controller

import "time"

// Action is what the controller did during one reconcile tick.
type Action string

const (
	// ActionScaleUp / ActionScaleDown changed the desired replica count.
	ActionScaleUp   Action = "scale_up"
	ActionScaleDown Action = "scale_down"
	// ActionNoop deliberately left the fleet unchanged; Reasons explains why.
	ActionNoop Action = "noop"
	// ActionError means a dependency (metric source, fleet, store) failed;
	// FailureClass classifies it and no fleet change was attempted.
	ActionError Action = "error"
)

// Reason is one machine-readable justification attached to a decision.
// Scale decisions may carry administrative reasons (rate limit, ...); a noop
// decision always carries at least one noop reason.
type Reason string

const (
	// Noop reasons — each is one of the required "reasons not to act".
	ReasonNoopNoFreshMetrics   Reason = "NO_FRESH_METRICS"    // nothing reported within the stale skew
	ReasonNoopFreshFractionLow Reason = "FRESH_FRACTION_LOW"  // too few instances reported fresh; uncertainty too high
	ReasonNoopWithinTolerance  Reason = "WITHIN_TOLERANCE"    // average utilisation is inside the ±deadband
	ReasonNoopWindowPending    Reason = "WINDOW_PENDING"      // scale-down level not observed for the full stable window
	ReasonNoopZeroNoDemand     Reason = "ZERO_NO_DEMAND"      // fleet is 0 and no demand signal exists
	ReasonNoopZeroDemandStale  Reason = "ZERO_DEMAND_STALE"   // fleet is 0 but the only demand signal is stale
	ReasonNoopMinFloor         Reason = "MIN_REPLICAS_FLOOR"  // target below MinReplicas, fleet already at the floor
	ReasonNoopMaxCapped        Reason = "MAX_REPLICAS_CAPPED" // target above MaxReplicas, fleet already at the cap

	// Scale-administrative reasons (may accompany a scale action).
	ReasonScaleUpRateLimited Reason = "UP_RATE_LIMITED"     // desired exceeded the per-tick scale-up bound
	ReasonFromZeroBootstrap  Reason = "FROM_ZERO_BOOTSTRAP" // scale from 0 used the bootstrap policy
)

// FailureClass categorises ActionError outcomes. Independent fault tests
// assert on these exact values.
type FailureClass string

const (
	FailureMetricRead   FailureClass = "METRIC_READ_FAILED"
	FailureFleetRead    FailureClass = "FLEET_READ_FAILED"
	FailureAdapterApply FailureClass = "ADAPTER_APPLY_FAILED"
	FailureStore        FailureClass = "STORE_FAILED"
	FailureInvalidInput FailureClass = "INVALID_INPUT"
)

// SampleView is one classified instance metric as seen by the decision.
type SampleView struct {
	InstanceID string  `json:"instance_id"`
	Load       float64 `json:"load"`
	ReportedAt int64   `json:"reported_at"`
	AgeSeconds int64   `json:"age_seconds"`
	Status     string  `json:"status"`    // "fresh" | "stale" | "missing"
	UsedLoad   float64 `json:"used_load"` // load actually fed into aggregation; imputed T for stale/missing
}

// Observation is the explainable input side of one tick: what was reported,
// how it was classified, and the resulting aggregate.
type Observation struct {
	At               int64        `json:"at"`
	CurrentReplicas  int          `json:"current_replicas"`
	FleetIDs         []string     `json:"fleet_ids"`
	Samples          []SampleView `json:"samples"`
	FreshCount       int          `json:"fresh_count"`
	StaleCount       int          `json:"stale_count"`
	MissingCount     int          `json:"missing_count"`
	FreshFraction    float64      `json:"fresh_fraction"`
	MinFreshFraction float64      `json:"min_fresh_fraction"`
	// Aggregation: fresh loads + one imputed T per stale/missing instance.
	FreshLoadSum      float64 `json:"fresh_load_sum"`
	ImputedLoadSum    float64 `json:"imputed_load_sum"`
	TotalLoad         float64 `json:"total_load"`
	AverageLoad       float64 `json:"average_load"`
	TargetPerInstance float64 `json:"target_load_per_instance"`
	UtilisationRatio  float64 `json:"utilisation_ratio"`
	RawDesired        int     `json:"raw_desired"`     // ceil(total/T), before rate limiting
	ClampedDesired    int     `json:"clamped_desired"` // after min/max clamp and tolerance deadband
	DemandPresent     *bool   `json:"demand_present,omitempty"`
	DemandReportedAt  int64   `json:"demand_reported_at,omitempty"`
	DemandStale       *bool   `json:"demand_stale,omitempty"`
}

// Decision is the fully explainable output of one reconcile tick. It is the
// row persisted to SQLite and the body returned over HTTP.
type Decision struct {
	ID               int64        `json:"id,omitempty"`
	RequestID        string       `json:"request_id"`
	TickAt           int64        `json:"tick_at"`
	Action           Action       `json:"action"`
	CurrentReplicas  int          `json:"current_replicas"`
	DesiredReplicas  int          `json:"desired_replicas"`
	PreviousReplicas int          `json:"previous_replicas,omitempty"`
	Reasons          []Reason     `json:"reasons"`
	FailureClass     FailureClass `json:"failure_class,omitempty"`
	FailureDetail    string       `json:"failure_detail,omitempty"`
	Observation      *Observation `json:"observation,omitempty"`
}

// RawPoint is one entry of the scale-down evidence history.
type RawPoint struct {
	At         int64 `json:"at"`
	RawDesired int   `json:"raw_desired"`
}

// AtTime returns t as unix-second timestamp helpers callers can format.
func AtTime(t int64) time.Time { return time.Unix(t, 0).UTC() }
