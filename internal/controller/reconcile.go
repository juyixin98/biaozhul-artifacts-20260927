package controller

import (
	"context"
	"fmt"
	"math"
	"sort"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/model"
)

// epsilon guards floating-point comparisons and ceiling arithmetic.
const epsilon = 1e-9

// retentionMargin keeps pruned reports slightly beyond the policy horizons so
// that an "expired report" stays distinguishable from "never reported".
const retentionMargin = 5 * time.Minute

// Engine is the reconciliation loop. It is transport- and storage-agnostic:
// all side effects go through Port, all time through Clock.
type Engine struct {
	port  Port
	clock Clock
}

// NewEngine builds an Engine. A nil clock defaults to the wall clock.
func NewEngine(port Port, clock Clock) *Engine {
	if clock == nil {
		clock = WallClock{}
	}
	return &Engine{port: port, clock: clock}
}

// Reconcile executes one tick and always returns a fully populated decision
// record, even when an actuator call fails (that failure is recorded on the
// decision instead of being swallowed). Infrastructure errors (unreadable
// config/fleet) are returned because no decision could be formed.
func (e *Engine) Reconcile(ctx context.Context, requestID string) (*model.Decision, error) {
	now := e.clock.Now()

	cfg, revision, err := e.port.LoadConfig(ctx)
	if err != nil {
		return nil, fmt.Errorf("load config: %w", err)
	}
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("stored configuration invalid: %w", err)
	}
	fleet, err := e.port.Fleet(ctx)
	if err != nil {
		return nil, fmt.Errorf("load fleet: %w", err)
	}

	current := fleet.Replicas()
	d := &model.Decision{
		RequestID:       requestID,
		TickAt:          now,
		ConfigVersion:   config.SchemaVersion,
		ConfigRevision:  revision,
		CurrentReplicas: current,
		AppliedReplicas: current,
		Metric:          cfg.Metric,
		TargetLoad:      cfg.TargetLoadPerInstance,
		Tolerance:       cfg.Tolerance,
		Location:        "internal/controller.Reconcile",
	}

	samples, err := e.port.LatestSamples(ctx, now)
	if err != nil {
		return nil, fmt.Errorf("load samples: %w", err)
	}
	if err := e.port.PruneSamples(ctx, now.Add(-(cfg.MetricFreshness.Duration + cfg.ScaleDownStableWindow.Duration + retentionMargin))); err != nil {
		return nil, fmt.Errorf("prune samples: %w", err)
	}
	if pruner, ok := e.port.(observationPruner); ok {
		horizon := cfg.ScaleDownStableWindow.Duration + retentionMargin
		if err := pruner.PruneObservations(ctx, now.Add(-horizon)); err != nil {
			return nil, fmt.Errorf("prune observations: %w", err)
		}
	}

	active := append([]string(nil), fleet.Instances...)
	sort.Strings(active)
	d.ActiveInstances = active
	classifyAndAggregate(d, samples, now, cfg)

	switch {
	case current == 0:
		e.scaleFromZero(ctx, d, cfg, now)
	case len(d.FreshInstances) == 0:
		// Expired or absent indicators are refused as a scaling basis.
		holdWhenNoFreshData(d, cfg)
	default:
		e.compute(ctx, d, cfg, now)
	}

	e.actuate(ctx, d, now)

	if err := e.port.SaveDecision(ctx, *d); err != nil {
		return nil, fmt.Errorf("save decision: %w", err)
	}
	return d, nil
}

// classifyAndAggregate partitions the active fleet using the latest report
// per instance and simultaneously sums fresh measured load:
//
//	observed at >= now-freshness => fresh (value participates in aggregation)
//	an older report exists        => stale (late/delayed report, excluded)
//	no report at all              => missing (unreported; conservatively
//	                                 imputed at target load)
func classifyAndAggregate(d *model.Decision, samples []model.Sample, now time.Time, cfg config.Config) {
	latest := make(map[string]model.Sample)
	for _, s := range samples {
		if cur, ok := latest[s.InstanceID]; !ok || s.ObservedAt.After(cur.ObservedAt) {
			latest[s.InstanceID] = s
		}
	}
	var measuredSum float64
	freshSet, staleSet := map[string]bool{}, map[string]bool{}
	for _, id := range d.ActiveInstances {
		s, reported := latest[id]
		switch {
		case reported && !s.ObservedAt.Before(now.Add(-cfg.MetricFreshness.Duration)):
			freshSet[id] = true
			measuredSum += s.Value
		case reported:
			staleSet[id] = true
		}
	}
	d.FreshInstances = d.FreshInstances[:0]
	d.StaleInstances = d.StaleInstances[:0]
	for id := range freshSet {
		d.FreshInstances = append(d.FreshInstances, id)
	}
	for id := range staleSet {
		d.StaleInstances = append(d.StaleInstances, id)
	}
	sort.Strings(d.FreshInstances)
	sort.Strings(d.StaleInstances)
	d.MissingInstances = d.MissingInstances[:0]
	for _, id := range d.ActiveInstances {
		if !freshSet[id] && !staleSet[id] {
			d.MissingInstances = append(d.MissingInstances, id)
		}
	}
	d.MeasuredLoad = measuredSum
	d.ImputedLoad = float64(len(d.MissingInstances)) * cfg.TargetLoadPerInstance
	d.TotalLoad = measuredSum + d.ImputedLoad
}

// scaleFromZero implements the independent zero-replica policy: load/target
// is undefined at zero, so a fresh external demand signal (pending work)
// bootstraps to BootstrapReplicas; otherwise we deliberately hold at zero.
func (e *Engine) scaleFromZero(ctx context.Context, d *model.Decision, cfg config.Config, now time.Time) {
	d.DesiredRaw = 0
	d.DesiredReplicas = 0
	if !cfg.ScaleFromZeroEnabled {
		addReason(d, ReasonZeroNoDemand, "scale-from-zero policy disabled; holding at 0")
		return
	}
	demand, ok, err := e.port.LatestDemand(ctx, now)
	if err != nil {
		addReason(d, ReasonZeroNoDemand, fmt.Sprintf("demand signal unreadable: %v; holding at 0", err))
		return
	}
	if !ok {
		addReason(d, ReasonZeroNoDemand, "no demand report received; holding at 0")
		return
	}
	if now.Sub(demand.ObservedAt) > cfg.DemandFreshness.Duration {
		addReason(d, ReasonZeroNoDemand,
			fmt.Sprintf("demand report expired (age %s > %s); holding at 0",
				now.Sub(demand.ObservedAt).Truncate(time.Second), cfg.DemandFreshness.Duration))
		return
	}
	if demand.Pending <= 0 {
		addReason(d, ReasonZeroNoDemand, "fresh demand signal reports zero pending work; holding at 0")
		return
	}
	want := cfg.BootstrapReplicas
	if want > cfg.MaxReplicas {
		want = cfg.MaxReplicas
		addReason(d, ReasonClampedMax, fmt.Sprintf("bootstrap count capped to max_replicas=%d", cfg.MaxReplicas))
	}
	d.DesiredRaw = want
	d.DesiredReplicas = want
	addReason(d, ReasonZeroBootstrapped,
		fmt.Sprintf("fresh demand of %d pending work item(s) at zero replicas; bootstrapping to %d", demand.Pending, want))
}

// holdWhenNoFreshData covers the all-stale and all-missing cases. Expired
// metrics must never trigger a scale up; with no usable positive signal the
// conservative action is to hold.
func holdWhenNoFreshData(d *model.Decision, cfg config.Config) {
	d.DesiredRaw = d.CurrentReplicas
	d.DesiredReplicas = d.CurrentReplicas
	if len(d.StaleInstances) > 0 {
		addReason(d, ReasonAllMetricsStale,
			fmt.Sprintf("no fresh sample for any of %d instances; expired metrics cannot trigger scale up; holding at %d",
				len(d.ActiveInstances), d.CurrentReplicas))
		addReason(d, ReasonStaleInstancesPresent,
			fmt.Sprintf("%d instance(s) reported only expired samples", len(d.StaleInstances)))
		d.Uncertainties = append(d.Uncertainties, UncertaintyStaleInstances)
	}
	if len(d.MissingInstances) > 0 {
		addReason(d, ReasonMissingBlocksDownscale,
			fmt.Sprintf("%d of %d instances unreported; cannot prove they carry no load; holding at %d",
				len(d.MissingInstances), len(d.ActiveInstances), d.CurrentReplicas))
		d.Uncertainties = append(d.Uncertainties, UncertaintyMissingInstances)
	}
}

// compute runs the normal formula + guard pipeline when fresh data exists.
func (e *Engine) compute(ctx context.Context, d *model.Decision, cfg config.Config, now time.Time) {
	current := d.CurrentReplicas
	target := cfg.TargetLoadPerInstance
	ratio := d.TotalLoad / (float64(current) * target)
	d.UsageRatio = &ratio

	// Hysteresis band, then the target formula:
	//   desired = ceil(totalLoad/target) when over-utilised (scale up),
	//             floor(...)            when under-utilised (scale down).
	var desired int32
	switch {
	case math.Abs(ratio-1.0) <= cfg.Tolerance+epsilon:
		desired = current
		addReason(d, ReasonWithinTolerance,
			fmt.Sprintf("usage ratio %.4f within ±%.0f%% band of 1.0; no scaling", ratio, cfg.Tolerance*100))
	case ratio < 1.0:
		desired = int32(math.Floor(d.TotalLoad/target + epsilon))
	default:
		desired = int32(math.Ceil(d.TotalLoad/target - epsilon))
	}

	// Conservative missing-instance guard: imputed instances already appear at
	// target load; additionally a hard rule forbids shrinking whenever the
	// fleet view is incomplete, since an unreported instance may be failed but
	// still in the traffic path.
	if len(d.MissingInstances) > 0 {
		d.Uncertainties = append(d.Uncertainties, UncertaintyMissingInstances)
		if desired < current {
			floor := int32(math.Floor(d.TotalLoad/target + epsilon))
			desired = current
			addReason(d, ReasonMissingBlocksDownscale,
				fmt.Sprintf("%d unreported instance(s); formula suggested %d, downscale refused; holding at %d",
					len(d.MissingInstances), floor, current))
		}
	}
	if len(d.StaleInstances) > 0 {
		d.Uncertainties = append(d.Uncertainties, UncertaintyStaleInstances)
		addReason(d, ReasonStaleInstancesPresent,
			fmt.Sprintf("%d expired report(s) excluded from aggregation", len(d.StaleInstances)))
	}
	if len(d.FreshInstances) < len(d.ActiveInstances) {
		d.Uncertainties = append(d.Uncertainties, UncertaintyPartialCoverage)
	}

	// Bounds.
	if desired < cfg.MinReplicas {
		desired = cfg.MinReplicas
		addReason(d, ReasonClampedMin, fmt.Sprintf("recommendation floored to min_replicas=%d", cfg.MinReplicas))
	}
	if desired > cfg.MaxReplicas {
		desired = cfg.MaxReplicas
		addReason(d, ReasonClampedMax, fmt.Sprintf("recommendation capped to max_replicas=%d", cfg.MaxReplicas))
	}

	d.DesiredRaw = desired
	d.DesiredReplicas = desired

	switch {
	case desired == current:
		addReason(d, ReasonDesiredEqualsCurrent,
			fmt.Sprintf("computed desired count equals current count %d", current))
	case desired > current:
		e.applyUpscaleRate(d, cfg)
	default:
		e.applyDownscaleWindow(ctx, d, now, cfg)
	}
}

func addReason(d *model.Decision, code, msg string) {
	d.Reasons = append(d.Reasons, model.Reason{Code: code, Message: msg})
}
