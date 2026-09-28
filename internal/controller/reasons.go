package controller

// Failure / blocking category codes. These are the stable, machine-readable
// vocabulary used by decisions and asserted on by tests (tests never match
// on free-form message text).
const (
	// ReasonZeroBootstrapped: independent scale-from-zero policy fired.
	ReasonZeroBootstrapped = "ZERO_BOOTSTRAPPED"
	// ReasonZeroNoDemand: fleet at zero but no fresh external demand signal.
	ReasonZeroNoDemand = "ZERO_NO_FRESH_DEMAND"

	// ReasonAllMetricsStale: no instance has a fresh sample; expired
	// indicators are refused as a basis for scaling up, so we hold.
	ReasonAllMetricsStale = "ALL_METRICS_STALE_HOLD"
	// ReasonMissingBlocksDownscale: at least one expected instance lacks a
	// fresh report (possibly failed); conservatively assume it is still
	// carrying target load, which forbids shrinking.
	ReasonMissingBlocksDownscale = "MISSING_INSTANCE_BLOCKS_DOWNSCALE"
	// ReasonStaleInstancesPresent: one or more latest reports are expired.
	ReasonStaleInstancesPresent = "STALE_INSTANCES_PRESENT"

	// ReasonWithinTolerance: load sits inside the hysteresis band; no action.
	ReasonWithinTolerance = "USAGE_RATIO_WITHIN_TOLERANCE"
	// ReasonDesiredEqualsCurrent: recomputation produced the current count.
	ReasonDesiredEqualsCurrent = "DESIRED_EQUALS_CURRENT"

	// ReasonDownscaleWindowNotMet: shrink recommendation has not persisted
	// for the full stable window; hold current count this tick.
	ReasonDownscaleWindowNotMet = "DOWNSCALE_STABLE_WINDOW_NOT_MET"
	// ReasonDownscaleWindowFirstObservation: no prior recommendations exist;
	// a shrink is deferred rather than applied on a single reading.
	ReasonDownscaleWindowFirstObservation = "DOWNSCALE_FIRST_OBSERVATION_DEFER"

	// ReasonClampedMin / Max: bound floor/ceiling clipped the formula result.
	ReasonClampedMin = "CLAMPED_TO_MIN_REPLICAS"
	ReasonClampedMax = "CLAMPED_TO_MAX_REPLICAS"
	// ReasonScaleUpRateLimited: per-tick growth cap clipped the formula.
	ReasonScaleUpRateLimited = "SCALE_UP_RATE_LIMITED"
)

// Uncertainty category codes: inputs that are suspect rather than blocking.
const (
	UncertaintyMissingInstances = "missing-instances-assumed-target-load"
	UncertaintyStaleInstances   = "stale-reports-excluded-from-aggregation"
	UncertaintyPartialCoverage  = "aggregation-based-on-partial-fresh-coverage"
)
