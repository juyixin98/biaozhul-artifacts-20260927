// Package controller implements the reconciliation loop of the local replica
// controller. It is deliberately free of I/O: all external participants are
// interfaces (metric source, fleet adapter, history/decision stores), so the
// algorithm is fully deterministically testable with a synthetic clock.
package controller

import (
	"fmt"
	"math"

	"replicactl/core/model"
)

// MetricSource provides the latest load samples of fleet members.
type MetricSource interface {
	// LatestSamples returns one entry per current fleet instance. An instance
	// that never reported comes back with Missing=true and zero Load.
	LatestSamples(now int64) ([]model.Sample, error)
	// LatestDemand returns the most recent out-of-band demand signal. ok==false
	// means no signal was ever posted.
	LatestDemand(now int64) (signal model.DemandSignal, ok bool, err error)
}

// Fleet is the local replica-set fixture the controller is allowed to mutate.
type Fleet interface {
	CurrentReplicas() (int, error)
	SetReplicas(n int) error
}

// DecisionStore persists one explainable row per reconcile tick and survives
// process restarts.
type DecisionStore interface {
	AppendDecision(d Decision) (Decision, error)
}

// RawHistory holds the per-tick pre-rate-limit desired levels used as
// evidence by the scale-down stable window. It is separate from
// DecisionStore so a tick's evidence is durable *before* the decision for that
// tick is evaluated, and so it survives restarts on its own.
type RawHistory interface {
	AppendPoint(p RawPoint) error
	// RawPointsSince returns points with at in [since, now], oldest first.
	RawPointsSince(since, now int64) ([]RawPoint, error)
}

// Controller runs one reconcile tick at a time.
type Controller struct {
	cfg     model.Config
	metrics MetricSource
	fleet   Fleet
	store   DecisionStore
	history RawHistory
}

// New constructs a controller after validating its configuration.
func New(cfg model.Config, metrics MetricSource, fleet Fleet, store DecisionStore, history RawHistory) (*Controller, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &Controller{cfg: cfg, metrics: metrics, fleet: fleet, store: store, history: history}, nil
}

const (
	statusFresh   = "fresh"
	statusStale   = "stale"
	statusMissing = "missing"
)

// Reconcile performs a single tick at now (unix seconds). Policy outcomes
// (noop reasons, scales) are returned as decisions; the method only returns a
// Go error when a dependency call itself fails and the decision is tagged
// ActionError with a FailureClass.
func (c *Controller) Reconcile(now int64, requestID string) (Decision, error) {
	dec := Decision{RequestID: requestID, TickAt: now, Reasons: []Reason{}}

	current, err := c.fleet.CurrentReplicas()
	if err != nil {
		return c.fail(dec, FailureFleetRead, err)
	}
	dec.CurrentReplicas = current

	// Zero-replica path has an independent policy: zero instances cannot
	// produce metrics, so only the out-of-band demand signal can wake it.
	if current == 0 {
		return c.reconcileFromZero(dec, now)
	}

	obs, err := c.observe(now, current)
	if err != nil {
		return c.fail(dec, FailureMetricRead, err)
	}
	dec.Observation = obs

	// The raw (pre-rate-limit, pre-deadband) desired level is durable
	// evidence for the scale-down window regardless of how this tick decides.
	if err := c.history.AppendPoint(RawPoint{At: now, RawDesired: obs.RawDesired}); err != nil {
		return c.fail(dec, FailureStore, err)
	}

	// ---- Conservative gating -------------------------------------------------
	// Stale metrics must not trigger a scale-up; missing reports were imputed
	// at target load. If too few fresh reports exist the uncertainty is too
	// high to act in either direction.
	if obs.FreshCount == 0 {
		return c.noop(dec, ReasonNoopNoFreshMetrics)
	}
	if obs.FreshFraction < c.cfg.MinFreshFraction {
		return c.noop(dec, ReasonNoopFreshFractionLow)
	}

	// ---- Tolerance deadband --------------------------------------------------
	// Average utilisation within 1±tolerance means the fleet is right-sized.
	lo, hi := 1-c.cfg.Tolerance, 1+c.cfg.Tolerance
	if obs.UtilisationRatio >= lo && obs.UtilisationRatio <= hi {
		return c.noop(dec, ReasonNoopWithinTolerance)
	}

	desired := obs.ClampedDesired

	switch {
	case desired > current:
		// ---- Scale-up, rate limited independently of the downscale window ----
		limited := scaleUpCeiling(current, c.cfg.MaxScaleUpFactor, c.cfg.MaxScaleUpFloor)
		target := desired
		if target > limited {
			target = limited
			dec.Reasons = append(dec.Reasons, ReasonScaleUpRateLimited)
		}
		if target > c.cfg.MaxReplicas {
			target = c.cfg.MaxReplicas
		}
		if target == current {
			return c.noop(dec, ReasonNoopMaxCapped)
		}
		return c.apply(dec, ActionScaleUp, target)

	case desired < current:
		// ---- Scale-down, governed by its own stable window -------------------
		windowStart := now - c.cfg.ScaleDownStableWindow
		points, err := c.history.RawPointsSince(windowStart, now)
		if err != nil {
			return c.fail(dec, FailureStore, err)
		}
		// The whole window must be covered by recorded ticks. Scaling down on
		// an incomplete window (cold start, restart onto a fresh database)
		// would skip the stability guarantee.
		if len(points) == 0 || points[0].At > windowStart {
			return c.noop(dec, ReasonNoopWindowPending)
		}
		// Conservative downscale level: the largest raw desired seen in the
		// window. A single still-busy tick keeps the fleet large.
		stableMax := desired
		for _, p := range points {
			if p.RawDesired > stableMax {
				stableMax = p.RawDesired
			}
		}
		if stableMax > c.cfg.MaxReplicas {
			stableMax = c.cfg.MaxReplicas
		}
		if stableMax >= current {
			return c.noop(dec, ReasonNoopWindowPending)
		}
		return c.apply(dec, ActionScaleDown, stableMax)

	default:
		// desired == current outside the deadband can only be a clamp outcome.
		if obs.RawDesired > c.cfg.MaxReplicas {
			return c.noop(dec, ReasonNoopMaxCapped)
		}
		return c.noop(dec, ReasonNoopMinFloor)
	}
}

// reconcileFromZero implements the dedicated zero-replica policy.
func (c *Controller) reconcileFromZero(dec Decision, now int64) (Decision, error) {
	signal, ok, err := c.metrics.LatestDemand(now)
	if err != nil {
		return c.fail(dec, FailureMetricRead, err)
	}
	obs := &Observation{At: now, CurrentReplicas: 0, TargetPerInstance: c.cfg.TargetLoadPerInstance,
		Samples: []SampleView{}, FleetIDs: []string{}}
	dec.Observation = obs

	if !ok || !signal.Present {
		if ok {
			b := signal.Present
			obs.DemandPresent = &b
			obs.DemandReportedAt = signal.ReportedAt
		}
		return c.noop(dec, ReasonNoopZeroNoDemand)
	}
	stale := now-signal.ReportedAt > c.cfg.StaleSkew
	obs.DemandPresent = &signal.Present
	obs.DemandReportedAt = signal.ReportedAt
	obs.DemandStale = &stale
	if stale {
		return c.noop(dec, ReasonNoopZeroDemandStale)
	}

	dec.Reasons = append(dec.Reasons, ReasonFromZeroBootstrap)
	return c.apply(dec, ActionScaleUp, c.cfg.BootstrapReplicas)
}

// observe reads the metric source, classifies every fleet member and computes
// the aggregate plus raw/clamped desired level.
func (c *Controller) observe(now int64, current int) (*Observation, error) {
	samples, err := c.metrics.LatestSamples(now)
	if err != nil {
		return nil, err
	}
	obs := &Observation{
		At:                now,
		CurrentReplicas:   current,
		TargetPerInstance: c.cfg.TargetLoadPerInstance,
		MinFreshFraction:  c.cfg.MinFreshFraction,
		Samples:           []SampleView{},
		FleetIDs:          []string{},
	}
	seen := map[string]bool{}
	for _, s := range samples {
		if seen[s.InstanceID] {
			return nil, fmt.Errorf("metric source returned duplicate instance %q", s.InstanceID)
		}
		seen[s.InstanceID] = true
		obs.FleetIDs = append(obs.FleetIDs, s.InstanceID)

		v := SampleView{InstanceID: s.InstanceID, Load: s.Load, ReportedAt: s.ReportedAt}
		switch {
		case s.Missing:
			v.Status = statusMissing
			obs.MissingCount++
		case now-s.ReportedAt > c.cfg.StaleSkew:
			v.Status = statusStale
			v.AgeSeconds = now - s.ReportedAt
			obs.StaleCount++
		default:
			v.Status = statusFresh
			v.AgeSeconds = now - s.ReportedAt
			obs.FreshCount++
		}
		if v.Status == statusFresh {
			v.UsedLoad = s.Load
			obs.FreshLoadSum += s.Load
		} else {
			// Conservative imputation: a non-reporting instance is assumed to
			// carry exactly target load — never zero (which would manufacture
			// a scale-down) and never its stale last value (old metrics must
			// not be leveraged into a scale-up).
			v.UsedLoad = c.cfg.TargetLoadPerInstance
			obs.ImputedLoadSum += c.cfg.TargetLoadPerInstance
		}
		obs.Samples = append(obs.Samples, v)
	}
	if len(samples) != current {
		return nil, fmt.Errorf("metric source returned %d samples for a fleet of %d", len(samples), current)
	}

	obs.TotalLoad = obs.FreshLoadSum + obs.ImputedLoadSum
	obs.AverageLoad = obs.TotalLoad / float64(current)
	obs.UtilisationRatio = obs.AverageLoad / c.cfg.TargetLoadPerInstance
	obs.FreshFraction = float64(obs.FreshCount) / float64(current)

	// Target formula (HPA-style): raw = ceil(totalLoad / T).
	obs.RawDesired = int(math.Ceil(obs.TotalLoad/c.cfg.TargetLoadPerInstance - epsilon))
	obs.ClampedDesired = clamp(obs.RawDesired, c.cfg.MinReplicas, c.cfg.MaxReplicas)
	return obs, nil
}

// apply performs the fleet mutation and persists the finished decision.
func (c *Controller) apply(dec Decision, action Action, target int) (Decision, error) {
	dec.Action = action
	dec.DesiredReplicas = target
	if err := c.fleet.SetReplicas(target); err != nil {
		return c.fail(dec, FailureAdapterApply, err)
	}
	saved, err := c.store.AppendDecision(dec)
	if err != nil {
		return c.fail(dec, FailureStore, err)
	}
	return saved, nil
}

// noop finalises a decision without mutating the fleet and persists it.
func (c *Controller) noop(dec Decision, reason Reason) (Decision, error) {
	dec.Action = ActionNoop
	dec.DesiredReplicas = dec.CurrentReplicas
	dec.Reasons = append(dec.Reasons, reason)
	saved, err := c.store.AppendDecision(dec)
	if err != nil {
		return c.fail(dec, FailureStore, err)
	}
	return saved, nil
}

func (c *Controller) fail(dec Decision, class FailureClass, cause error) (Decision, error) {
	dec.Action = ActionError
	dec.DesiredReplicas = dec.CurrentReplicas
	dec.FailureClass = class
	dec.FailureDetail = cause.Error()
	return dec, cause
}

// scaleUpCeiling is the per-tick scale-up bound: max(floor(current*factor),
// current+floor): the multiplicative limit with a +floor absolute guarantee.
func scaleUpCeiling(current int, factor, floor float64) int {
	byFactor := int(math.Floor(float64(current) * factor))
	byFloor := current + int(math.Floor(floor+epsilon))
	if byFloor > byFactor {
		return byFloor
	}
	return byFactor
}

func clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

const epsilon = 1e-9
