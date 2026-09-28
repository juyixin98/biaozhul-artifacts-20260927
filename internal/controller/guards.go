package controller

import (
	"context"
	"fmt"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/model"
)

// applyUpscaleRate enforces the independently maintained scale-up rate limit
// (factor cap and absolute delta cap). Scale ups are immediate once capped.
func (e *Engine) applyUpscaleRate(d *model.Decision, cfg config.Config) {
	ceiling := cfg.ScaleUpCeiling(d.CurrentReplicas)
	if d.DesiredReplicas > ceiling {
		addReason(d, ReasonScaleUpRateLimited,
			fmt.Sprintf("formula desired %d but one-tick ceiling is %d (factor %.1fx + max-delta %d from current %d)",
				d.DesiredReplicas, ceiling, cfg.ScaleUpMaxFactor, cfg.ScaleUpMaxDelta, d.CurrentReplicas))
		d.DesiredReplicas = ceiling
	}
	// A degenerate configuration could pin the ceiling at current; in that
	// case no capacity changes and the decision reads as an explicit no-op.
	if d.DesiredReplicas == d.CurrentReplicas {
		d.Action = "none"
		addReason(d, ReasonDesiredEqualsCurrent,
			fmt.Sprintf("rate-limit ceiling %d leaves replica count unchanged", d.CurrentReplicas))
	}
}

// applyDownscaleWindow enforces the conservative shrink window, maintained
// separately from the scale-up rate limit (Kubernetes-style stabilization).
//
//   - record this tick's post-hysteresis, post-bounds recommendation;
//   - a scale down is applied only when a recommendation at or below the
//     candidate has existed continuously for the full window: there must be
//     an "anchor" reading at least one full window old, AND every reading
//     inside the window (age <= window) must be at or below the candidate.
//     A short low reading / one-off spike recovery therefore cannot shrink
//     capacity; a single observation with no history defers.
//
// Readings older than the window are retained as anchors but do not raise the
// window maximum (a high reading before the window must not block a shrink
// whose whole window was calm).
func (e *Engine) applyDownscaleWindow(ctx context.Context, d *model.Decision, now time.Time, cfg config.Config) {
	window := cfg.ScaleDownStableWindow.Duration
	candidate := d.DesiredReplicas

	if err := e.port.SaveObservation(ctx, now, candidate); err != nil {
		// Cannot establish stability -> conservative hold.
		d.DesiredReplicas = d.CurrentReplicas
		addReason(d, ReasonDownscaleWindowNotMet,
			fmt.Sprintf("failed to record recommendation for stable window: %v; holding at %d", err, d.CurrentReplicas))
		return
	}

	if window <= 0 {
		return // stable window disabled
	}

	obs, err := e.port.ObservationsSince(ctx, now.Add(-window-retentionMargin))
	if err != nil {
		d.DesiredReplicas = d.CurrentReplicas
		addReason(d, ReasonDownscaleWindowNotMet,
			fmt.Sprintf("failed to read stable window history: %v; holding at %d", err, d.CurrentReplicas))
		return
	}

	// Exclude the observation just saved at `now` when evaluating the anchor;
	// it is the current candidate and cannot itself prove the window elapsed.
	var spanning bool
	windowMax := candidate
	for _, o := range obs {
		age := now.Sub(o.At)
		if age >= window {
			spanning = true // an anchor reading a full window old exists
		}
		if age <= window && age > 0 && o.Replicas > windowMax {
			windowMax = o.Replicas // only readings inside the window raise the bar
		}
	}
	if !spanning {
		if len(obs) <= 1 {
			addReason(d, ReasonDownscaleWindowFirstObservation,
				fmt.Sprintf("first below-target reading (%d); deferring shrink until it persists for %s", candidate, window))
		} else {
			addReason(d, ReasonDownscaleWindowNotMet,
				fmt.Sprintf("below-target recommendation has not persisted for the full %s window; holding at %d", window, d.CurrentReplicas))
		}
		d.DesiredReplicas = d.CurrentReplicas
		return
	}
	if windowMax > candidate {
		addReason(d, ReasonDownscaleWindowNotMet,
			fmt.Sprintf("max recommendation within window is %d (> candidate %d); shrinking only to %d",
				windowMax, candidate, windowMax))
		d.DesiredReplicas = windowMax
	}
	if d.DesiredReplicas >= d.CurrentReplicas {
		// Window history pushed the target back up to current: no action.
		addReason(d, ReasonDesiredEqualsCurrent,
			fmt.Sprintf("stable-window target %d equals current %d", d.DesiredReplicas, d.CurrentReplicas))
	}
}

// actuate applies the final desired count to the fleet through the Port.
// Any actuator error is recorded on the decision (failure category
// ACTUATOR_ERROR) and the applied count stays at the pre-call value, so the
// outcome is honest rather than silently assumed successful.
func (e *Engine) actuate(ctx context.Context, d *model.Decision, now time.Time) {
	switch {
	case d.DesiredReplicas > d.CurrentReplicas:
		d.Action = "scale_up"
	case d.DesiredReplicas < d.CurrentReplicas:
		d.Action = "scale_down"
	default:
		d.Action = "none"
	}
	if d.DesiredReplicas == d.CurrentReplicas {
		return
	}
	newActive, added, removed, err := e.port.ApplyScale(ctx, d.DesiredReplicas, now)
	if err != nil {
		d.ActuatorError = err.Error()
		d.DesiredReplicas = d.CurrentReplicas
		d.Action = "none"
		addReason(d, "ACTUATOR_ERROR",
			fmt.Sprintf("fleet actuator refused scale to %d: %v; fleet remains at %d", d.DesiredReplicas, err, d.CurrentReplicas))
		return
	}
	d.AppliedReplicas = int32(len(newActive))
	d.ScaledUpIDs = added
	d.ScaledDownIDs = removed
}
