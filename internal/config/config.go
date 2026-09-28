// Package config defines the tunable policy of the replica controller.
//
// All policy decisions from the behavioural contract are explicit fields
// here, with documented defaults. The controller never hard-codes policy
// constants inside the reconciliation loop.
package config

import (
	"encoding/json"
	"errors"
	"time"
)

// SchemaVersion is bumped whenever the configuration schema or the decision
// semantics change. It is echoed on every decision record.
const SchemaVersion = 1

// Duration is a time.Duration that serializes as a human readable string
// ("30s", "5m") so that configuration files stay auditable.
type Duration struct{ time.Duration }

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.String())
}

func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	if v < 0 {
		return errors.New("duration must not be negative")
	}
	d.Duration = v
	return nil
}

// Config is the complete policy configuration.
type Config struct {
	// Metric selects which reported load metric drives scaling.
	Metric string `json:"metric"`
	// TargetLoadPerInstance is the desired load each instance should carry.
	TargetLoadPerInstance float64 `json:"target_load_per_instance"`

	// MinReplicas / MaxReplicas bound the actuator.
	MinReplicas int32 `json:"min_replicas"`
	MaxReplicas int32 `json:"max_replicas"`

	// MetricFreshness is the maximum age (at tick time) of a sample that may
	// influence scaling. Anything older is "stale": stale metrics must not
	// trigger a scale up.
	MetricFreshness Duration `json:"metric_freshness"`
	// DemandFreshness is the equivalent bound for the scale-from-zero signal.
	DemandFreshness Duration `json:"demand_freshness"`

	// ScaleDownStableWindow is the conservative shrink window. A scale down
	// is applied only when the recommended replica count has been at or below
	// the new value continuously for this long. Maintained separately from
	// the scale-up rate limit.
	ScaleDownStableWindow Duration `json:"scale_down_stable_window"`
	// ScaleUpMaxFactor bounds one tick's growth as a multiple of current
	// replicas (Kubernetes-style burst allowance).
	ScaleUpMaxFactor float64 `json:"scale_up_max_factor"`
	// ScaleUpMaxDelta bounds one tick's growth by an absolute number.
	ScaleUpMaxDelta int32 `json:"scale_up_max_delta"`
	// Tolerance is the hysteresis band: |usageRatio-1| <= tolerance means no
	// scaling (0.1 == ±10%).
	Tolerance float64 `json:"tolerance"`

	// BootstrapReplicas is the independent scale-from-zero policy: when the
	// fleet is at zero and a fresh external demand signal is present, jump to
	// this many replicas instead of dividing load by the target (which is
	// undefined at zero).
	BootstrapReplicas int32 `json:"bootstrap_replicas"`
	// ScaleFromZeroEnabled gates that policy.
	ScaleFromZeroEnabled bool `json:"scale_from_zero_enabled"`

	// InitialReplicas seeds the fleet when the database is first created.
	InitialReplicas int32 `json:"initial_replicas"`

	// TickInterval is the background reconciliation cadence.
	TickInterval Duration `json:"tick_interval"`

	// ListenAddr is the HTTP server address.
	ListenAddr string `json:"listen_addr"`
}

// Default returns the documented baseline configuration.
func Default() Config {
	return Config{
		Metric:                "requests_per_second",
		TargetLoadPerInstance: 100,
		MinReplicas:           0,
		MaxReplicas:           20,

		MetricFreshness: Duration{60 * time.Second},
		DemandFreshness: Duration{60 * time.Second},

		ScaleDownStableWindow: Duration{5 * time.Minute},
		ScaleUpMaxFactor:      2.0,
		ScaleUpMaxDelta:       4,
		Tolerance:             0.10,

		BootstrapReplicas:    1,
		ScaleFromZeroEnabled: true,

		InitialReplicas: 3,
		TickInterval:    Duration{15 * time.Second},
		ListenAddr:      "127.0.0.1:8080",
	}
}

// Validate checks policy invariants. It returns a joined error describing
// every invalid field so that a rejected configuration is fully explained.
func (c Config) Validate() error {
	var errs []error
	if c.Metric == "" {
		errs = append(errs, errors.New("metric must not be empty"))
	}
	if c.TargetLoadPerInstance <= 0 {
		errs = append(errs, errors.New("target_load_per_instance must be > 0"))
	}
	if c.MinReplicas < 0 {
		errs = append(errs, errors.New("min_replicas must be >= 0"))
	}
	if c.MaxReplicas < c.MinReplicas {
		errs = append(errs, errors.New("max_replicas must be >= min_replicas"))
	}
	if c.MetricFreshness.Duration <= 0 {
		errs = append(errs, errors.New("metric_freshness must be > 0"))
	}
	if c.DemandFreshness.Duration <= 0 {
		errs = append(errs, errors.New("demand_freshness must be > 0"))
	}
	if c.ScaleDownStableWindow.Duration < 0 {
		errs = append(errs, errors.New("scale_down_stable_window must be >= 0"))
	}
	if c.ScaleUpMaxFactor < 1 {
		errs = append(errs, errors.New("scale_up_max_factor must be >= 1"))
	}
	if c.ScaleUpMaxDelta < 0 {
		errs = append(errs, errors.New("scale_up_max_delta must be >= 0"))
	}
	if c.Tolerance < 0 || c.Tolerance >= 1 {
		errs = append(errs, errors.New("tolerance must be in [0,1)"))
	}
	if c.BootstrapReplicas < 1 {
		errs = append(errs, errors.New("bootstrap_replicas must be >= 1"))
	}
	if c.TickInterval.Duration < 0 {
		errs = append(errs, errors.New("tick_interval must be >= 0"))
	}
	if c.MaxReplicas > 0 && c.BootstrapReplicas > c.MaxReplicas {
		errs = append(errs, errors.New("bootstrap_replicas must be <= max_replicas"))
	}
	return errors.Join(errs...)
}

// ScaleUpCeiling computes the largest replica count reachable in one tick
// from current, applying both the factor and the absolute delta caps.
func (c Config) ScaleUpCeiling(current int32) int32 {
	factorCap := int64(float64(current)*c.ScaleUpMaxFactor + 0.999999) // ceil
	deltaCap := int64(current) + int64(c.ScaleUpMaxDelta)
	ceil := factorCap
	if deltaCap < ceil {
		ceil = deltaCap
	}
	if ceil < int64(current) {
		ceil = int64(current)
	}
	return int32(ceil)
}
