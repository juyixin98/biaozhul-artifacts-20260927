// Package reference is an INDEPENDENT re-statement of the controller policy
// used only by the acceptance harness. It must never import
// replicactl/core or replicactl/app: it exists precisely so the expected
// outcomes are not produced by the code under test. The reference is written
// from the behavioural contract (see docs/ALGORITHM.md), not from the
// production sources.
//
// Differences from production are intentional in shape (single-file state
// machine, dense synthetic ticks) but identical in policy: same formula,
// same conservative imputation, same gates, same window and rate limit.
package reference

// Params are the contract parameters.
type Params struct {
	Target      float64 // target load per replica T
	UpFactor    float64 // per-tick multiplicative scale-up bound
	UpFloor     float64 // absolute extra-replica guarantee
	DownWindow  int64   // scale-down stable window (seconds)
	StaleSkew   int64   // staleness skew (seconds)
	Tolerance   float64 // deadband fraction
	MinFresh    float64 // minimum fresh fraction to act
	MinReplicas int
	MaxReplicas int
	Bootstrap   int
}

// DefaultParams mirrors config/controller.json.
func DefaultParams() Params {
	return Params{
		Target: 10, UpFactor: 2, UpFloor: 1, DownWindow: 60,
		StaleSkew: 30, Tolerance: 0.10, MinFresh: 0.5,
		MinReplicas: 0, MaxReplicas: 16, Bootstrap: 1,
	}
}

// Outcome is the hand-checkable expected result of one reconcile tick.
type Outcome struct {
	Action          string
	DesiredReplicas int
	// Reasons contains the exact expected reason set. A scale action carries
	// the empty administrative reason unless the up-rate limit is hit.
	Reasons []string
	// RawDesired is the expected pre-rate-limit target formula result.
	RawDesired int
	// Fresh/Stale/Missing are the expected classification counts.
	Fresh, Stale, Missing int
	// TotalLoad is the expected aggregate fed to the formula.
	TotalLoad float64
}

// Sim is the independent state machine.
type Sim struct {
	p        Params
	replicas int
	latest   map[string]sample
	demand   demandSig
	// history maps tick time -> raw desired (one point per tick).
	history map[int64]int
}

type sample struct {
	load       float64
	reportedAt int64
}

type demandSig struct {
	has, present bool
	at           int64
}

// NewSim starts a simulation at initialReplicas with an empty history —
// exactly what a fresh process sees. Restart calls must construct a new Sim
// and call Restore with the prior state's durable contents.
func NewSim(p Params, initialReplicas int) *Sim {
	return &Sim{p: p, replicas: initialReplicas, latest: map[string]sample{}, history: map[int64]int{}}
}

// Replicas returns the current simulated fleet size.
func (s *Sim) Replicas() int { return s.replicas }

// PutSample stores a report at synthetic time now (late reports allowed).
func (s *Sim) PutSample(id string, load float64, reportedAt, now int64) {
	s.latest[id] = sample{load: load, reportedAt: reportedAt}
}

// PutDemand stores the latest out-of-band demand signal.
func (s *Sim) PutDemand(present bool, at int64) {
	s.demand = demandSig{has: true, present: present, at: at}
}

// State is the durable subset carried across a simulated restart.
type State struct {
	Replicas int
	Latest   map[string]sampleExport
	Demand   demandExport
	History  map[int64]int
}

type sampleExport struct {
	Load       float64
	ReportedAt int64
}
type demandExport struct {
	Has, Present bool
	At           int64
}

// Export returns durable state.
func (s *Sim) Export() State {
	st := State{Replicas: s.replicas, History: map[int64]int{}}
	st.Latest = map[string]sampleExport{}
	for k, v := range s.latest {
		st.Latest[k] = sampleExport{Load: v.load, ReportedAt: v.reportedAt}
	}
	if s.demand.has {
		st.Demand = demandExport{Has: true, Present: s.demand.present, At: s.demand.at}
	}
	for k, v := range s.history {
		st.History[k] = v
	}
	return st
}

// Restore builds a fresh-process Sim from previously exported state,
// replicating what SQLite restores after a service restart.
func Restore(p Params, st State) *Sim {
	s := NewSim(p, st.Replicas)
	for k, v := range st.Latest {
		s.latest[k] = sample{load: v.Load, reportedAt: v.ReportedAt}
	}
	if st.Demand.Has {
		s.demand = demandSig{has: true, present: st.Demand.Present, at: st.Demand.At}
	}
	for k, v := range st.History {
		s.history[k] = v
	}
	return s
}

// Reconcile advances one tick at now and returns the expected outcome.
func (s *Sim) Reconcile(now int64) Outcome {
	if s.replicas == 0 {
		return s.fromZero(now)
	}

	n := s.replicas
	fresh, stale, missing := 0, 0, 0
	var total float64
	for i := 1; i <= n; i++ {
		id := instID(i)
		rep, ok := s.latest[id]
		switch {
		case !ok:
			missing++
			total += s.p.Target // conservative imputation at T
		case now-rep.reportedAt > s.p.StaleSkew:
			stale++
			total += s.p.Target
		default:
			fresh++
			total += rep.load
		}
	}
	freshFraction := float64(fresh) / float64(n)
	avg := total / float64(n)
	util := avg / s.p.Target
	raw := ceilDiv(total, s.p.Target)
	clamped := raw
	if clamped < s.p.MinReplicas {
		clamped = s.p.MinReplicas
	}
	if clamped > s.p.MaxReplicas {
		clamped = s.p.MaxReplicas
	}
	// Evidence for the stable window is durable regardless of the decision.
	s.history[now] = raw

	o := Outcome{DesiredReplicas: s.replicas, RawDesired: raw,
		Fresh: fresh, Stale: stale, Missing: missing, TotalLoad: total}

	if fresh == 0 {
		return o.with("noop", s.replicas, "NO_FRESH_METRICS")
	}
	if freshFraction < s.p.MinFresh {
		return o.with("noop", s.replicas, "FRESH_FRACTION_LOW")
	}
	if util >= 1-s.p.Tolerance && util <= 1+s.p.Tolerance {
		return o.with("noop", s.replicas, "WITHIN_TOLERANCE")
	}

	switch {
	case clamped > n:
		byFactor := int(float64(n) * s.p.UpFactor)
		byFloor := n + int(s.p.UpFloor)
		ceiling := byFactor
		if byFloor > ceiling {
			ceiling = byFloor
		}
		reasons := []string{}
		target := clamped
		if target > ceiling {
			target = ceiling
			reasons = append(reasons, "UP_RATE_LIMITED")
		}
		if target > s.p.MaxReplicas {
			target = s.p.MaxReplicas
		}
		if target == n {
			return o.with("noop", n, "MAX_REPLICAS_CAPPED")
		}
		s.replicas = target
		return o.with("scale_up", target, reasons...)
	case clamped < n:
		// Window must be fully covered. Production queries evidence in
		// [now-window, now] and requires the oldest point to sit at (not after)
		// the window start. With dense scenario ticks that is exactly the
		// first tick whose distance reaches the window length.
		windowStart := now - s.p.DownWindow
		if _, covered := s.history[windowStart]; !covered {
			return o.with("noop", n, "WINDOW_PENDING")
		}
		// Conservative level = max raw desired across the window.
		level := clamped
		for at, v := range s.history {
			if at >= now-s.p.DownWindow && at <= now && v > level {
				level = v
			}
		}
		if level > s.p.MaxReplicas {
			level = s.p.MaxReplicas
		}
		if level >= n {
			return o.with("noop", n, "WINDOW_PENDING")
		}
		s.replicas = level
		return o.with("scale_down", level)
	default:
		if raw > s.p.MaxReplicas {
			return o.with("noop", n, "MAX_REPLICAS_CAPPED")
		}
		return o.with("noop", n, "MIN_REPLICAS_FLOOR")
	}
}

func (s *Sim) fromZero(now int64) Outcome {
	o := Outcome{DesiredReplicas: 0}
	if !s.demand.has || !s.demand.present {
		return o.with("noop", 0, "ZERO_NO_DEMAND")
	}
	if now-s.demand.at > s.p.StaleSkew {
		return o.with("noop", 0, "ZERO_DEMAND_STALE")
	}
	s.replicas = s.p.Bootstrap
	return o.with("scale_up", s.p.Bootstrap, "FROM_ZERO_BOOTSTRAP")
}

func (o Outcome) with(action string, desired int, reasons ...string) Outcome {
	o.Action = action
	o.DesiredReplicas = desired
	o.Reasons = reasons
	if o.Reasons == nil {
		o.Reasons = []string{}
	}
	return o
}

func ceilDiv(total, target float64) int {
	v := total / target
	k := int(v)
	if float64(k) < v-1e-9 {
		k++
	}
	return k
}

func instID(i int) string {
	// Local copy; kept dependency-free by design.
	const digits = "0123456789"
	var b [12]byte
	b[0] = 'i'
	b[1] = 'n'
	b[2] = 's'
	b[3] = 't'
	b[4] = 'a'
	b[5] = 'n'
	b[6] = 'c'
	b[7] = 'e'
	b[8] = '-'
	b[9] = digits[(i/100)%10]
	b[10] = digits[(i/10)%10]
	b[11] = digits[i%10]
	return string(b[:])
}
