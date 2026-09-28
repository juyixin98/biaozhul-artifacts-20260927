// Package plan turns (desired, observed) into an ordered, evidence-bound plan.
//
// A plan has two phases:
//
//	Phase A destroy — deletes and the "old" half of replaces, executed in
//	                  reverse dependency order (dependents first).
//	Phase B create  — creates, in-place updates and the "new" half of
//	                  replaces, executed in forward dependency order.
//
// Every plan binds to the observation run number and the observation digest.
// The apply step re-observes and refuses on mismatch (drift → state conflict).
package plan

import (
	"fmt"
	"sort"

	"infraplanner/internal/dag"
	"infraplanner/internal/errorsx"
	"infraplanner/internal/model"
	"infraplanner/internal/registry"
)

// Status of a stored plan.
type Status string

const (
	StatusPlanned  Status = "planned"
	StatusApplying Status = "applying"
	StatusApplied  Status = "applied"
	StatusFailed   Status = "failed"
	StatusRejected Status = "rejected" // apply refused (drift / protection)
)

// Step is one unit of work against one resource.
type Step struct {
	Ref       model.Ref      `json:"ref"`
	Action    model.Action   `json:"action"`
	Phase     string         `json:"phase"` // "A" destroy or "B" create
	Order     int            `json:"order"` // execution index within plan
	Before    map[string]string `json:"before,omitempty"`
	After     map[string]string `json:"after,omitempty"`
	DependsOn []string       `json:"depends_on,omitempty"`
	// ChangedAttrs are mutable differences for an update.
	ChangedAttrs []string `json:"changed_attrs,omitempty"`
	// ImmutableChanges lists immutable attributes forcing a replace.
	ImmutableChanges []string `json:"immutable_changes,omitempty"`
	// Protected indicates the actual resource is protected; RequiresRelease
	// names the ref that must appear in desired.release_protection.
	Protected       bool `json:"protected,omitempty"`
	RequiresRelease bool `json:"requires_release,omitempty"`
	// Recovery marks a step whose observed state was non-terminal when planned;
	// the engine must Read before acting and must never blindly Create.
	Recovery bool `json:"recovery,omitempty"`
	// Reason is the human judgment recorded for evidence.
	Reason string `json:"reason"`
}

// Plan is the diff + order + evidence binding.
type Plan struct {
	ID         string  `json:"plan_id"`
	Run        int64   `json:"observation_run"`
	BoundDigest string `json:"bound_observation_digest"`
	DesiredDigest string `json:"desired_digest"`
	Status     Status  `json:"status"`
	Steps      []*Step `json:"steps"`
	// Summary is persisted as part of the evidence record.
	Summary Summary `json:"summary"`
}

// Summary counts actions and records protected/refused notes.
type Summary struct {
	Creates       int `json:"creates"`
	Updates       int `json:"updates"`
	Replaces      int `json:"replaces"`
	Deletes       int `json:"deletes"`
	Noops         int `json:"noops"`
	Recoveries    int `json:"recoveries"`
	Protected     int `json:"protected_seen"`
	ExternalLeft  int `json:"external_untouched"`
}

// Planner computes plans. Stateless: state lives in storage/reconcile.
type Planner struct{}

func New() *Planner { return &Planner{} }

// Build computes a plan against an observation. desired is already canonical.
func (p *Planner) Build(planID string, desired []model.Spec, desiredRaw *model.DesiredSet, obs *model.ObservedSet, desiredDigest string) (*Plan, error) {
	g, err := buildUnionGraph(desired, obs)
	if err != nil {
		return nil, err
	}
	createOrder, err := g.CreateOrder()
	if err != nil {
		return nil, err
	}
	idx := map[string]int{}
	for i, id := range createOrder {
		idx[id] = i
	}

	desiredByID := map[string]model.Spec{}
	for _, s := range desired {
		desiredByID[s.ID] = s
	}

	var stepsA, stepsB []*Step
	sum := Summary{}

	// Pass over observed: updates / replaces / deletes / recoveries / noops.
	obsKeys := make([]string, 0, len(obs.Resources))
	for k := range obs.Resources {
		obsKeys = append(obsKeys, k)
	}
	sort.Strings(obsKeys)

	// track ids needing destroy (delete + replace) for reverse ordering
	destroySet := map[string]bool{}
	type intent struct {
		stepA, stepB *Step
	}
	intents := map[string]*intent{}

	for _, k := range obsKeys {
		res := obs.Resources[k]
		want, inDesired := desiredByID[res.Ref.ID]
		if !inDesired {
			if res.External {
				sum.ExternalLeft++
				continue
			}
			// destroy
			protected := res.Protected
			if protected {
				sum.Protected++
			}
			s := &Step{
				Ref: res.Ref, Action: model.ActionDelete, Phase: "A",
				Before: copyMap(res.Attrs), Protected: protected,
				RequiresRelease: protected,
				Reason: "resource absent from desired set; destroy in reverse dependency order",
			}
			intents[res.Ref.ID] = &intent{stepA: s}
			destroySet[res.Ref.ID] = true
			sum.Deletes++
			continue
		}

		// desired + observed: kind identity cannot change (id maps to one kind)
		if want.Kind != res.Ref.Kind {
			return nil, errorsx.Compute("KIND_MISMATCH",
				fmt.Sprintf("resource %q kind changed from %s to %s; kind is part of identity",
					res.Ref.ID, res.Ref.Kind, want.Kind),
				map[string]any{"id": res.Ref.ID, "observed": string(res.Ref.Kind), "desired": string(want.Kind)})
		}

		def, _ := registry.Get(want.Kind)
		immSet := def.ImmutableSet()
		var immutableChanged, mutableChanged []string

		attrKeys := map[string]bool{}
		for k := range res.Attrs {
			attrKeys[k] = true
		}
		for k := range want.Attrs {
			attrKeys[k] = true
		}
		for a := range attrKeys {
			if res.Attrs[a] != want.Attrs[a] {
				if immSet[a] {
					immutableChanged = append(immutableChanged, a)
				} else {
					mutableChanged = append(mutableChanged, a)
				}
			}
		}
		sort.Strings(immutableChanged)
		sort.Strings(mutableChanged)

		nonTerminal := res.State != model.StateExists
		protected := res.Protected || want.Protected
		if protected {
			sum.Protected++
		}

		it := &intent{}

		switch {
		case nonTerminal:
			// A prior run was interrupted. Plan the converged action; engine
			// Reads reality and never blindly re-creates.
			rec := &Step{
				Ref: res.Ref, Phase: "B", After: copyMap(want.Attrs),
				Before: copyMap(res.Attrs), DependsOn: append([]string(nil), want.DependsOn...),
				Recovery: true, Protected: protected,
				Reason: fmt.Sprintf("observed non-terminal state %q; recover by reading real outcome before any write", res.State),
			}
			switch res.State {
			case model.StateDeleting:
				rec.Action = model.ActionCreate
				rec.Reason = "interrupted delete on a still-desired resource: confirm gone, then create"
				sum.Creates++
			case model.StateCreating, model.StateCreateLost, model.StateUpdating, model.StateReplacing:
				if len(immutableChanged) > 0 {
					rec.Action = model.ActionReplace
					rec.ImmutableChanges = immutableChanged
					rec.RequiresRelease = protected
					sum.Replaces++
				} else if len(mutableChanged) > 0 {
					rec.Action = model.ActionUpdate
					rec.ChangedAttrs = mutableChanged
					sum.Updates++
				} else {
					rec.Action = model.ActionNoop
					sum.Noops++
				}
			}
			sum.Recoveries++
			it.stepB = rec
		case len(immutableChanged) > 0:
			// replace: destroy old (A) then create new (B), same identity
			old := &Step{
				Ref: res.Ref, Action: model.ActionReplace, Phase: "A",
				Before: copyMap(res.Attrs), DependsOn: append([]string(nil), want.DependsOn...),
				Protected: protected, RequiresRelease: protected,
				ImmutableChanges: immutableChanged,
				Reason: "replace half 1/2: remove old instance after immutable change, reverse order",
			}
			newS := &Step{
				Ref: res.Ref, Action: model.ActionReplace, Phase: "B", Order: idx[res.Ref.ID],
				After: copyMap(want.Attrs), Before: copyMap(res.Attrs),
				DependsOn: append([]string(nil), want.DependsOn...),
				ImmutableChanges: immutableChanged,
				Reason: "replace half 2/2: create new instance preserving identity, forward order",
			}
			it.stepA, it.stepB = old, newS
			destroySet[res.Ref.ID] = true
			sum.Replaces++
		case len(mutableChanged) > 0:
			it.stepB = &Step{
				Ref: res.Ref, Action: model.ActionUpdate, Phase: "B", Order: idx[res.Ref.ID],
				Before: copyMap(res.Attrs), After: copyMap(want.Attrs),
				DependsOn: append([]string(nil), want.DependsOn...),
				ChangedAttrs: mutableChanged,
				Reason: "in-place update of mutable attributes",
			}
			sum.Updates++
		default:
			it.stepB = &Step{
				Ref: res.Ref, Action: model.ActionNoop, Phase: "B", Order: idx[res.Ref.ID],
				Before: copyMap(res.Attrs), After: copyMap(want.Attrs),
				DependsOn: append([]string(nil), want.DependsOn...),
				Reason: "already converges; no write",
			}
			sum.Noops++
		}
		intents[res.Ref.ID] = it
	}

	// Pass over desired-only: creates.
	for _, s := range desired {
		if _, seen := obs.Resources[s.Ref().String()]; seen {
			continue
		}
		protected := s.Protected
		if protected {
			sum.Protected++
		}
		intents[s.ID] = &intent{stepB: &Step{
			Ref: model.Ref{Kind: s.Kind, ID: s.ID}, Action: model.ActionCreate, Phase: "B",
			Order: idx[s.ID], After: copyMap(s.Attrs),
			DependsOn: append([]string(nil), s.DependsOn...),
			Protected: protected,
			Reason: "new resource; create after dependencies",
		}}
		sum.Creates++
	}

	// Ordering: phase A over destroySet in reverse topological order;
	// phase B over createOrder restricted to steps that write.
	destroyIDs := make([]string, 0, len(destroySet))
	for id := range destroySet {
		destroyIDs = append(destroyIDs, id)
	}
	delOrder, err := g.SubsetDeleteOrder(destroyIDs)
	if err != nil {
		return nil, err
	}
	ord := 0
	for _, id := range delOrder {
		s := intents[id].stepA
		s.Order = ord
		ord++
		stepsA = append(stepsA, s)
	}
	for _, id := range createOrder {
		it, ok := intents[id]
		if !ok || it.stepB == nil {
			continue
		}
		if it.stepB.Action == model.ActionNoop && !it.stepB.Recovery {
			// keep noops in plan for evidence but they still get an order
		}
		s := it.stepB
		s.Order = ord
		ord++
		stepsB = append(stepsB, s)
	}

	// Protection gate: destruction of a protected resource requires explicit
	// release in THIS desired input.
	var blocked []string
	for _, s := range stepsA {
		if s.RequiresRelease && !desiredRaw.ProtectionReleased(s.Ref.String()) {
			blocked = append(blocked, s.Ref.String())
		}
	}
	if len(blocked) > 0 {
		sort.Strings(blocked)
		return nil, errorsx.Protected("PROTECTION_HELD",
			"critical resource destruction refused; add refs to release_protection",
			map[string]any{"refs": blocked})
	}

	steps := append(stepsA, stepsB...)
	return &Plan{
		ID: planID, Run: obs.Run,
		BoundDigest:   "", // filled by caller (canonical.ObservationDigest)
		DesiredDigest: desiredDigest,
		Status:        StatusPlanned,
		Steps:         steps,
		Summary:       sum,
	}, nil
}

// buildUnionGraph merges desired dependency edges with observed dependency
// edges so that destroys of resources absent from desired still order
// correctly against each other.
func buildUnionGraph(desired []model.Spec, obs *model.ObservedSet) (*dag.Graph, error) {
	union := make([]model.Spec, 0, len(desired)+len(obs.Resources))
	edges := map[string]map[string]bool{}
	kinds := map[string]model.Kind{}

	addNode := func(id string, k model.Kind) {
		if _, ok := kinds[id]; !ok {
			kinds[id] = k
			edges[id] = map[string]bool{}
		}
	}
	addEdge := func(from, to string) { edges[from][to] = true }

	for _, s := range desired {
		addNode(s.ID, s.Kind)
	}
	for _, r := range obs.Resources {
		addNode(r.Ref.ID, r.Ref.Kind)
		for _, d := range r.DependsOn {
			addNode(d, r.Ref.Kind) // best-effort kind; edge validity checked below
		}
	}
	for _, s := range desired {
		for _, d := range s.DependsOn {
			addEdge(s.ID, d)
		}
	}
	for _, r := range obs.Resources {
		for _, d := range r.DependsOn {
			addEdge(r.Ref.ID, d)
		}
	}
	ids := make([]string, 0, len(kinds))
	for id := range kinds {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		deps := make([]string, 0, len(edges[id]))
		for d := range edges[id] {
			deps = append(deps, d)
		}
		sort.Strings(deps)
		union = append(union, model.Spec{ID: id, Kind: kinds[id], DependsOn: deps})
	}
	return dag.Build(union)
}

func copyMap(m map[string]string) map[string]string {
	if m == nil {
		return map[string]string{}
	}
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// WritableSteps returns steps the engine must act on (skips pure noops).
func (p *Plan) WritableSteps() []*Step {
	var out []*Step
	for _, s := range p.Steps {
		if s.Action == model.ActionNoop && !s.Recovery {
			continue
		}
		out = append(out, s)
	}
	return out
}
