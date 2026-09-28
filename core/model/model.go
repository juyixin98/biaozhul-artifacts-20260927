// Package model defines the resource model of the local replica controller:
// the fleet it controls, per-instance load samples and the controller's
// configuration. The package carries no behaviour beyond input validation —
// all decisions live in package controller.
package model

import (
	"errors"
	"fmt"
)

// LoadSample is one per-instance report: how many units of load (e.g. in-flight
// requests) the instance was carrying at ReportedAt.
type LoadSample struct {
	InstanceID string
	Load       float64
	ReportedAt int64 // unix seconds; may lag the observation time (late report)
}

// DemandSignal is an out-of-band indicator that work exists even while the
// fleet has zero instances (zero instances cannot report metrics themselves).
// Typical source: a synthetic queue depth report from the local fixture.
type DemandSignal struct {
	Present    bool  // some load is queued/requested
	ReportedAt int64 // unix seconds; subject to the same staleness rule as samples
}

// Sample is one classified observation of a single fleet instance, produced by
// the controller while reading its metric source. Fresh is false when the
// latest report is older than the staleness skew (or no report exists).
type Sample struct {
	InstanceID string
	Load       float64
	ReportedAt int64
	Fresh      bool
	Missing    bool // no report was ever registered for this instance
}

// Config is the full, validated controller configuration.
type Config struct {
	// TargetLoadPerInstance T: one replica is sized for this many load units.
	TargetLoadPerInstance float64
	// MaxScaleUpFactor bounds a single scale-up: desired <= current * factor.
	MaxScaleUpFactor float64
	// MaxScaleUpFloor guarantees at least one new replica can be added on
	// scale-up regardless of the multiplicative factor.
	MaxScaleUpFloor float64
	// ScaleDownStableWindow: a scale-down is emitted only after the raw
	// (pre-rate-limit) desired level has stayed at or below the proposed
	// smaller level for this long.
	ScaleDownStableWindow int64
	// StaleSkew: a sample/demand whose ReportedAt is older than now-skew is
	// stale and must not trigger a scale-up.
	StaleSkew int64
	// Tolerance is the deadband around the target average utilisation, as a
	// fraction (0.1 == ±10%). Within the band the desired level is "at target"
	// and no action is taken.
	Tolerance float64
	// MinFreshFraction: fraction of the fleet with fresh metrics required for
	// any decision that changes replica count. Below it the controller stays
	// put and reports uncertainty. Conservative: missing reports are assumed
	// to carry exactly target load, never zero.
	MinFreshFraction float64
	MinReplicas      int
	MaxReplicas      int
	// BootstrapReplicas is the level used when scaling up from zero on the
	// back of a fresh demand signal.
	BootstrapReplicas int
}

// Validate checks the configuration once at construction.
func (c Config) Validate() error {
	checks := []struct {
		ok  bool
		msg string
	}{
		{c.TargetLoadPerInstance > 0, "TargetLoadPerInstance must be > 0"},
		{c.MaxScaleUpFactor >= 1, "MaxScaleUpFactor must be >= 1"},
		{c.MaxScaleUpFloor >= 1, "MaxScaleUpFloor must be >= 1"},
		{c.ScaleDownStableWindow > 0, "ScaleDownStableWindow must be > 0"},
		{c.StaleSkew > 0, "StaleSkew must be > 0"},
		{c.Tolerance > 0 && c.Tolerance < 1, "Tolerance must be in (0,1)"},
		{c.MinFreshFraction > 0 && c.MinFreshFraction <= 1, "MinFreshFraction must be in (0,1]"},
		{c.MinReplicas >= 0, "MinReplicas must be >= 0"},
		{c.MaxReplicas >= c.MinReplicas, "MaxReplicas must be >= MinReplicas"},
		{c.BootstrapReplicas >= 1, "BootstrapReplicas must be >= 1"},
		{c.BootstrapReplicas >= c.MinReplicas, "BootstrapReplicas must be >= MinReplicas"},
		{c.BootstrapReplicas <= c.MaxReplicas, "BootstrapReplicas must be <= MaxReplicas"},
	}
	for _, ch := range checks {
		if !ch.ok {
			return errors.New("invalid controller config: " + ch.msg)
		}
	}
	return nil
}

// DefaultConfig returns the values used by every test fixture in this repo:
// T=10, 2x scale-up per tick (+1 floor), 60s scale-down window, 30s stale
// skew, 10% deadband, half the fleet must report fresh, range 0..16,
// bootstrap 1.
func DefaultConfig() Config {
	return Config{
		TargetLoadPerInstance: 10,
		MaxScaleUpFactor:      2,
		MaxScaleUpFloor:       1,
		ScaleDownStableWindow: 60,
		StaleSkew:             30,
		Tolerance:             0.10,
		MinFreshFraction:      0.5,
		MinReplicas:           0,
		MaxReplicas:           16,
		BootstrapReplicas:     1,
	}
}

// ValidateSample checks an inbound metric report at the adapter boundary.
func ValidateSample(s LoadSample, at int64) error {
	if s.InstanceID == "" {
		return errors.New("instance_id must not be empty")
	}
	if s.Load < 0 {
		return fmt.Errorf("load must be >= 0, got %v", s.Load)
	}
	if s.ReportedAt <= 0 {
		return errors.New("reported_at must be a positive unix timestamp")
	}
	if s.ReportedAt > at {
		return fmt.Errorf("reported_at %d is in the future (now %d)", s.ReportedAt, at)
	}
	return nil
}
