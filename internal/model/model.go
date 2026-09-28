// Package model holds the domain types shared by every module of the
// replica controller: resource model, samples, demand signals and the
// explainable decision record produced by each reconciliation tick.
package model

import "time"

// Fleet is the resource model: a local, synthetic fleet of instances.
// Instances carry stable, sequential identifiers (ins-0001, ins-0002, ...)
// that are never reused within the lifetime of the database, so a sample can
// always be attributed to exactly one lifecycle of an instance.
type Fleet struct {
	// Instances are the currently active instance IDs, ordered by their
	// allocation sequence. len(Instances) is the current replica count.
	Instances []string  `json:"instances"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Replicas returns the current replica count.
func (f Fleet) Replicas() int32 { return int32(len(f.Instances)) }

// Sample is one load report coming from a single instance.
type Sample struct {
	InstanceID string    `json:"instance_id"`
	Metric     string    `json:"metric"`
	Value      float64   `json:"value"`
	ObservedAt time.Time `json:"observed_at"` // time the load was measured on the instance
	ReceivedAt time.Time `json:"received_at"` // time the controller accepted the report
}

// Demand is the external work signal used solely for scaling from zero
// (for example queued requests waiting for a worker). It is a local fixture,
// never a real queue.
type Demand struct {
	Pending    int64     `json:"pending"`
	ObservedAt time.Time `json:"observed_at"`
	ReceivedAt time.Time `json:"received_at"`
}

// Reason is one machine-readable explanation attached to a decision.
// A decision that takes no scaling action always carries at least one reason
// describing the failure/blocking category that produced the no-op.
type Reason struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Decision is the fully explainable result of one reconciliation tick.
// Every numeric intermediate used by the formula is recorded so that an
// operator (or an acceptance test) can recompute the outcome by hand.
type Decision struct {
	RequestID string    `json:"request_id"`
	TickAt    time.Time `json:"tick_at"`

	// Versioning / provenance.
	ConfigVersion  int    `json:"config_version"`  // configuration schema version
	ConfigRevision int64  `json:"config_revision"` // incremented on every config replacement
	Location       string `json:"location"`        // code position that produced the decision

	// Replica counts through each pipeline stage.
	CurrentReplicas int32  `json:"current_replicas"`
	DesiredRaw      int32  `json:"desired_raw"`      // post-hysteresis + post-bounds recommendation (input to rate-limit/stable-window)
	DesiredReplicas int32  `json:"desired_replicas"` // final value sent to the actuator
	AppliedReplicas int32  `json:"applied_replicas"` // fleet size after actuation
	Action          string `json:"action"`           // scale_up | scale_down | none

	// Formula inputs/intermediates.
	Metric       string   `json:"metric"`
	TargetLoad   float64  `json:"target_load"`
	MeasuredLoad float64  `json:"measured_load"` // sum of fresh reported values
	TotalLoad    float64  `json:"total_load"`    // measured + conservative imputation for missing
	ImputedLoad  float64  `json:"imputed_load"`
	UsageRatio   *float64 `json:"usage_ratio,omitempty"` // total / (current*target); nil when undefined
	Tolerance    float64  `json:"tolerance"`

	// Data quality.
	ActiveInstances  []string `json:"active_instances"`
	FreshInstances   []string `json:"fresh_instances"`
	MissingInstances []string `json:"missing_instances"` // expected but with no fresh sample
	StaleInstances   []string `json:"stale_instances"`   // a report exists but it is expired

	// Blocking / failure categories and explicit uncertainty statements.
	Reasons       []Reason `json:"reasons"`
	Uncertainties []string `json:"uncertainties"`

	// Actuator effects.
	ScaledUpIDs   []string `json:"scaled_up_ids,omitempty"`
	ScaledDownIDs []string `json:"scaled_down_ids,omitempty"`
	ActuatorError string   `json:"actuator_error,omitempty"`
}
