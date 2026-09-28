// Package controller contains the reconciliation loop: the pure policy
// engine that turns a fleet state plus fresh load samples into an
// explainable scaling decision.
//
// The engine depends only on the Port interface (persistence/actuator),
// never on SQLite or HTTP, so it is fully deterministic under an injected
// clock and can be driven by unit, fault and acceptance tests alike.
package controller

import (
	"context"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/model"
)

// Clock abstracts time so tests can advance the scale-down window exactly.
type Clock interface{ Now() time.Time }

// WallClock is the production clock.
type WallClock struct{}

func (WallClock) Now() time.Time { return time.Now().UTC() }

// Port is everything the reconciliation engine needs from the outside
// world. It is implemented by the SQLite store plus the fleet actuator in
// the adapter layer, and by fault-injecting fakes in fault tests.
type Port interface {
	LoadConfig(ctx context.Context) (config.Config, int64, error)
	Fleet(ctx context.Context) (model.Fleet, error)
	LatestSamples(ctx context.Context, now time.Time) ([]model.Sample, error)
	LatestDemand(ctx context.Context, now time.Time) (model.Demand, bool, error)
	// PruneSamples discards reports older than the retention horizon.
	PruneSamples(ctx context.Context, before time.Time) error
	// SaveObservation records one post-hysteresis, post-bounds recommended
	// replica count for the downscale stabilization window.
	SaveObservation(ctx context.Context, at time.Time, replicas int32) error
	// ObservationsSince returns recorded recommendations with at >= since,
	// ordered oldest first.
	ObservationsSince(ctx context.Context, since time.Time) ([]Observation, error)
	// ApplyScale changes the fleet size and returns the new active instance
	// IDs (and the IDs added/removed), or an error which the decision records.
	ApplyScale(ctx context.Context, want int32, at time.Time) (newActive, added, removed []string, err error)
	// SaveDecision persists the explainable decision record.
	SaveDecision(ctx context.Context, d model.Decision) error
}

// Observation is one historical recommendation used by the stable window.
type Observation struct {
	At       time.Time
	Replicas int32
}

// observationPruner is an optional Port capability: stores that retain
// stable-window observations implement it so the engine can bound growth.
type observationPruner interface {
	PruneObservations(ctx context.Context, before time.Time) error
}
