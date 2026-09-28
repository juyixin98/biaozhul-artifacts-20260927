// Package oracle is the INDEPENDENT reference implementation used only by
// tests. It deliberately does NOT import internal/controller: it re-derives
// the expected decision straight from the behavioural specification and the
// concrete inputs of a scenario, so the system under test cannot be its own
// grader.
//
// The code style is intentionally plain and the arithmetic is spelled out
// step by step so that a reviewer can verify it by hand on paper.
package oracle

import (
	"math"
	"sort"
	"time"
)

// Policy is the subset of configuration the formula needs. It mirrors
// config.Config but is declared independently so the reference cannot
// accidentally track an implementation drift.
type Policy struct {
	TargetPerInstance float64
	MinReplicas       int32
	MaxReplicas       int32
	Freshness         time.Duration
	DemandFreshness   time.Duration
	StableWindow      time.Duration
	UpFactor          float64
	UpMaxDelta        int32
	Tolerance         float64
	Bootstrap         int32
	FromZeroEnabled   bool
}

// InstanceReport is the latest known report of one instance at tick time.
// ObservedAt == nil means "never reported". A report older than Freshness is
// "stale"; otherwise "fresh".
type InstanceReport struct {
	ID         string
	Value      float64
	ObservedAt *time.Time // nil => missing
}

// DemandState models the external work signal (nil Has => no report yet).
type DemandState struct {
	Has        bool
	Pending    int64
	ObservedAt time.Time
}

// PriorObservation is one recorded recommendation from before this tick.
type PriorObservation struct {
	At       time.Time
	Replicas int32
}

// Input fully describes one tick from the grader's point of view.
type Input struct {
	Tick    time.Time
	Current int32
	Active  []string
	Reports []InstanceReport
	Demand  DemandState
	Prior   []PriorObservation
	Policy  Policy
}

// Expectation is the hand-computable outcome the implementation must match.
type Expectation struct {
	Action          string
	DesiredReplicas int32 // after every guard, what the actuator is asked for
	RawDesired      int32 // formula output, pre-guards
	Fresh           []string
	Stale           []string
	Missing         []string
	TotalLoad       float64
	Ratio           *float64
	ReasonCodes     []string
	Uncertainties   []string
}

const eps = 1e-9

// anchorMargin is how far beyond the stable window the reference still looks
// for an anchor reading. It mirrors (independently, by value) the engine's
// retention horizon.
const anchorMargin = 5 * time.Minute

// Expected computes the reference result.
func Expected(in Input) Expectation {
	p := in.Policy
	now := in.Tick

	active := append([]string(nil), in.Active...)
	sort.Strings(active)

	// Latest report per active id.
	latest := map[string]InstanceReport{}
	for _, r := range in.Reports {
		if cur, ok := latest[r.ID]; !ok || (r.ObservedAt != nil && (cur.ObservedAt == nil || r.ObservedAt.After(*cur.ObservedAt))) {
			latest[r.ID] = r
		}
	}

	var fresh, stale, missing []string
	var measured float64
	for _, id := range active {
		r, has := latest[id]
		switch {
		case has && r.ObservedAt != nil && now.Sub(*r.ObservedAt) <= p.Freshness:
			fresh = append(fresh, id)
			measured += r.Value
		case has && r.ObservedAt != nil:
			stale = append(stale, id)
		default:
			missing = append(missing, id)
		}
	}
	sort.Strings(fresh)
	sort.Strings(stale)
	sort.Strings(missing)

	imputed := float64(len(missing)) * p.TargetPerInstance
	total := measured + imputed

	out := Expectation{Fresh: fresh, Stale: stale, Missing: missing, TotalLoad: total}
	add := func(c string) { out.ReasonCodes = append(out.ReasonCodes, c) }

	// --- independent scale-from-zero policy -------------------------------
	if in.Current == 0 {
		out.RawDesired, out.DesiredReplicas = 0, 0
		out.Action = "none"
		if !p.FromZeroEnabled {
			add("ZERO_NO_FRESH_DEMAND")
			return out
		}
		if !in.Demand.Has {
			add("ZERO_NO_FRESH_DEMAND")
			return out
		}
		if now.Sub(in.Demand.ObservedAt) > p.DemandFreshness || in.Demand.Pending <= 0 {
			add("ZERO_NO_FRESH_DEMAND")
			return out
		}
		want := p.Bootstrap
		if want > p.MaxReplicas {
			want = p.MaxReplicas
			add("CLAMPED_TO_MAX_REPLICAS")
		}
		out.RawDesired, out.DesiredReplicas = want, want
		out.Action = "scale_up"
		add("ZERO_BOOTSTRAPPED")
		return out
	}

	// --- no fresh data: stale metrics cannot trigger a scale up ----------
	if len(fresh) == 0 {
		out.RawDesired, out.DesiredReplicas = in.Current, in.Current
		out.Action = "none"
		if len(stale) > 0 {
			add("ALL_METRICS_STALE_HOLD")
			add("STALE_INSTANCES_PRESENT")
			out.Uncertainties = append(out.Uncertainties, "stale-reports-excluded-from-aggregation")
		}
		if len(missing) > 0 {
			add("MISSING_INSTANCE_BLOCKS_DOWNSCALE")
			out.Uncertainties = append(out.Uncertainties, "missing-instances-assumed-target-load")
		}
		return out
	}

	// --- formula -----------------------------------------------------------
	ratio := total / (float64(in.Current) * p.TargetPerInstance)
	out.Ratio = &ratio

	var desired int32
	switch {
	case math.Abs(ratio-1) <= p.Tolerance+eps:
		desired = in.Current
		add("USAGE_RATIO_WITHIN_TOLERANCE")
	case ratio < 1:
		desired = int32(math.Floor(total/p.TargetPerInstance + eps))
	default:
		desired = int32(math.Ceil(total/p.TargetPerInstance - eps))
	}

	if len(missing) > 0 {
		out.Uncertainties = append(out.Uncertainties, "missing-instances-assumed-target-load")
		if desired < in.Current {
			desired = in.Current
			add("MISSING_INSTANCE_BLOCKS_DOWNSCALE")
		}
	}
	if len(stale) > 0 {
		out.Uncertainties = append(out.Uncertainties, "stale-reports-excluded-from-aggregation")
		add("STALE_INSTANCES_PRESENT")
	}
	if len(fresh) < len(active) {
		out.Uncertainties = append(out.Uncertainties, "aggregation-based-on-partial-fresh-coverage")
	}

	if desired < p.MinReplicas {
		desired = p.MinReplicas
		add("CLAMPED_TO_MIN_REPLICAS")
	}
	if desired > p.MaxReplicas {
		desired = p.MaxReplicas
		add("CLAMPED_TO_MAX_REPLICAS")
	}
	out.RawDesired = desired
	out.DesiredReplicas = desired

	if desired == in.Current {
		add("DESIRED_EQUALS_CURRENT")
		out.Action = "none"
		return out
	}

	// --- independently maintained scale-up rate limit ---------------------
	if desired > in.Current {
		ceil := int32(math.Ceil(float64(in.Current) * p.UpFactor))
		deltaCap := in.Current + p.UpMaxDelta
		if deltaCap < ceil {
			ceil = deltaCap
		}
		if desired > ceil {
			desired = ceil
			add("SCALE_UP_RATE_LIMITED")
		}
		out.DesiredReplicas = desired
		if desired > in.Current {
			out.Action = "scale_up"
		} else {
			out.Action = "none"
			add("DESIRED_EQUALS_CURRENT")
		}
		return out
	}

	// --- downscale stable window ------------------------------------------
	// Kubernetes-style stabilization: an anchor reading at least one full
	// window old must exist, and every reading INSIDE the window
	// (0 < age <= window) must be at or below the candidate. Readings older
	// than the window anchor the span but do not raise the window maximum.
	candidate := desired
	obs := make([]PriorObservation, 0, len(in.Prior)+1)
	for _, o := range in.Prior {
		if !o.At.Before(now.Add(-(p.StableWindow + anchorMargin))) {
			obs = append(obs, o)
		}
	}
	retainedPrior := len(obs)
	obs = append(obs, PriorObservation{At: now, Replicas: candidate})

	spanning := false
	windowMax := candidate
	for _, o := range obs {
		age := now.Sub(o.At)
		if age >= p.StableWindow {
			spanning = true
		}
		if age <= p.StableWindow && age > 0 && o.Replicas > windowMax {
			windowMax = o.Replicas
		}
	}
	if !spanning {
		out.DesiredReplicas = in.Current
		out.Action = "none"
		// No prior history within the retention horizon => "first reading".
		if retainedPrior == 0 {
			add("DOWNSCALE_FIRST_OBSERVATION_DEFER")
		} else {
			add("DOWNSCALE_STABLE_WINDOW_NOT_MET")
		}
		return out
	}
	if windowMax > candidate {
		desired = windowMax
		add("DOWNSCALE_STABLE_WINDOW_NOT_MET")
	}
	out.DesiredReplicas = desired
	if desired < in.Current {
		out.Action = "scale_down"
	} else {
		out.Action = "none"
		add("DESIRED_EQUALS_CURRENT")
	}
	return out
}
