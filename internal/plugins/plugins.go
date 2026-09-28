// Package plugins contains the built-in mutators and validators plus the
// fixture-only plugins used by the failure tests (delay, flaky, quota,
// oscillating and rogue-path).
package plugins

import (
	"context"
	"fmt"
	"strconv"
	"time"

	"admission/internal/admission"
	"admission/internal/quantity"
	"admission/internal/types"
)

// ReplicaDefaulter fills /spec/replicas when it is absent. Re-entrant: when
// replicas is already set it returns an empty patch.
type ReplicaDefaulter struct {
	Default int
}

func (d *ReplicaDefaulter) Name() string { return "defaults.replicas" }

func (d *ReplicaDefaulter) AllowedPrefixes() []string { return []string{"/spec/replicas"} }

func (d *ReplicaDefaulter) Mutate(_ context.Context, obj *types.Object) (types.Patch, error) {
	if obj.Spec.Replicas != nil {
		return types.Patch{}, nil
	}
	n := d.Default
	if n <= 0 {
		n = 1
	}
	return types.Patch{Ops: []types.PatchOp{{Op: types.OpAdd, Path: "/spec/replicas", Value: n}}}, nil
}

// ResourcesDefaulter fills missing cpu/memory with defaults.
type ResourcesDefaulter struct {
	DefaultCPU    string
	DefaultMemory string
}

func (d *ResourcesDefaulter) Name() string { return "defaults.resources" }

func (d *ResourcesDefaulter) AllowedPrefixes() []string {
	return []string{"/spec/cpu", "/spec/memory"}
}

func (d *ResourcesDefaulter) Mutate(_ context.Context, obj *types.Object) (types.Patch, error) {
	var ops []types.PatchOp
	if obj.Spec.CPU == "" && d.DefaultCPU != "" {
		ops = append(ops, types.PatchOp{Op: types.OpAdd, Path: "/spec/cpu", Value: d.DefaultCPU})
	}
	if obj.Spec.Memory == "" && d.DefaultMemory != "" {
		ops = append(ops, types.PatchOp{Op: types.OpAdd, Path: "/spec/memory", Value: d.DefaultMemory})
	}
	return types.Patch{Ops: ops}, nil
}

// ReservedResources runs before LabelSync and projects the parsed cpu request
// into /spec/extra/reservedCPU. LabelSync in turn consumes this projection:
// the two plugins are the "mutually influencing" pair in the chain.
type ReservedResources struct{}

func (r *ReservedResources) Name() string { return "mutators.reserved-resources" }

func (r *ReservedResources) AllowedPrefixes() []string {
	return []string{"/spec/extra/reservedCPU"}
}

func (r *ReservedResources) Mutate(_ context.Context, obj *types.Object) (types.Patch, error) {
	if obj.Spec.CPU == "" {
		return types.Patch{}, nil
	}
	milli, err := quantity.ParseCPU(obj.Spec.CPU)
	if err != nil {
		// Bad input in a mutator is an input error with an explicit category.
		return types.Patch{}, &admission.PluginError{
			Category: types.CatInvalidInput, Plugin: r.Name(),
			Message: "cannot project cpu reservation: " + err.Error(),
		}
	}
	cur, _ := obj.Spec.Extra["reservedCPU"].(float64)
	if int(cur) == milli {
		return types.Patch{}, nil
	}
	return types.Patch{Ops: []types.PatchOp{{
		Op: types.OpAdd, Path: "/spec/extra/reservedCPU", Value: milli,
	}}}, nil
}

// LabelSync copies team/size facts into annotations, including the cpu
// projection produced by ReservedResources in an earlier plugin/pass. It is
// idempotent: existing correct annotations are left untouched.
type LabelSync struct{}

func (l *LabelSync) Name() string { return "mutators.label-sync" }

func (l *LabelSync) AllowedPrefixes() []string {
	return []string{"/metadata/annotations"}
}

func (l *LabelSync) Mutate(_ context.Context, obj *types.Object) (types.Patch, error) {
	want := map[string]string{}
	if team := obj.Metadata.Labels["team"]; team != "" {
		want["admission.example.com/team"] = team
	}
	if r, ok := obj.Spec.Extra["reservedCPU"]; ok {
		want["admission.example.com/reserved-cpu-milli"] = strconv.Itoa(toInt(r))
	}
	if len(want) == 0 {
		return types.Patch{}, nil
	}
	ann := obj.Metadata.Annotations
	if ann == nil {
		ann = map[string]string{}
	}
	var ops []types.PatchOp
	for k, v := range want {
		if cur, ok := ann[k]; ok && cur == v {
			continue
		}
		ops = append(ops, types.PatchOp{
			Op: types.OpAdd, Path: "/metadata/annotations/" + escapeToken(k), Value: v,
		})
	}
	return types.Patch{Ops: ops}, nil
}

// escapeToken applies RFC 6901 token escaping for annotation keys.
func escapeToken(t string) string {
	out := make([]byte, 0, len(t)+4)
	for i := 0; i < len(t); i++ {
		switch t[i] {
		case '~':
			out = append(out, '~', '0')
		case '/':
			out = append(out, '~', '1')
		default:
			out = append(out, t[i])
		}
	}
	return string(out)
}

// toInt converts a JSON-decoded or Go-native numeric value to int.
func toInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case int64:
		return int(n)
	}
	return 0
}

// ---- validators -----------------------------------------------------------

// ReplicaRangeValidator rejects out-of-range and missing replica counts.
// Missing/bad *values* are input errors (a malformed document); in-range but
// disallowed counts are a clean policy denial.
type ReplicaRangeValidator struct {
	Min int
	Max int
}

func (v *ReplicaRangeValidator) Name() string { return "validators.replica-range" }

func (v *ReplicaRangeValidator) Validate(_ context.Context, req types.Review) (types.Decision, error) {
	r := req.Object.Spec.Replicas
	if r == nil {
		return types.Decision{}, &admission.PluginError{
			Category: types.CatInvalidInput, Plugin: v.Name(),
			Message: "spec.replicas is required after defaulting",
		}
	}
	if *r < 0 {
		return types.Decision{}, &admission.PluginError{
			Category: types.CatInvalidInput, Plugin: v.Name(),
			Message: fmt.Sprintf("spec.replicas must not be negative, got %d", *r),
		}
	}
	if *r < v.Min || *r > v.Max {
		return types.Decision{Allowed: false, Reason: fmt.Sprintf(
			"spec.replicas=%d outside allowed range [%d,%d]", *r, v.Min, v.Max)}, nil
	}
	return types.Decision{Allowed: true}, nil
}

// ImmutableValidator enforces that core spec fields do not change while an
// object is flagged immutable. This is a state conflict, not a policy denial.
type ImmutableValidator struct{}

func (v *ImmutableValidator) Name() string { return "validators.immutable-fields" }

func (v *ImmutableValidator) Validate(_ context.Context, req types.Review) (types.Decision, error) {
	if req.Operation != "UPDATE" || req.OldObject == nil {
		return types.Decision{Allowed: true}, nil
	}
	if !req.OldObject.Spec.Immutable {
		return types.Decision{Allowed: true}, nil
	}
	old, cur := req.OldObject.Spec, req.Object.Spec
	if old.CPU != cur.CPU {
		return types.Decision{}, immutableErr(v.Name(), "spec.cpu", old.CPU, cur.CPU)
	}
	if old.Memory != cur.Memory {
		return types.Decision{}, immutableErr(v.Name(), "spec.memory", old.Memory, cur.Memory)
	}
	if replicasChanged(old.Replicas, cur.Replicas) {
		return types.Decision{}, immutableErr(v.Name(), "spec.replicas",
			replicaRep(old.Replicas), replicaRep(cur.Replicas))
	}
	return types.Decision{Allowed: true}, nil
}

func replicasChanged(a, b *int) bool {
	if a == nil || b == nil {
		return a != b
	}
	return *a != *b
}

func replicaRep(r *int) string {
	if r == nil {
		return "<unset>"
	}
	return strconv.Itoa(*r)
}

func immutableErr(plugin, field string, oldv, curv any) *admission.PluginError {
	return &admission.PluginError{
		Category: types.CatStateConflict, Plugin: plugin,
		Message: fmt.Sprintf("immutable field %s changed: %v -> %v", field, oldv, curv),
	}
}

// ---- fixture plugins ------------------------------------------------------

// DelayPlugin sleeps longer than its deadline so the pipeline records a
// distinguishable Timeout failure. It honors context cancellation promptly.
type DelayPlugin struct {
	Name_        string
	Delay        time.Duration
	AllowedPaths []string
}

func (d *DelayPlugin) Name() string              { return d.Name_ }
func (d *DelayPlugin) AllowedPrefixes() []string { return d.AllowedPaths }
func (d *DelayPlugin) Mutate(ctx context.Context, _ *types.Object) (types.Patch, error) {
	select {
	case <-time.After(d.Delay):
		return types.Patch{}, nil
	case <-ctx.Done():
		return types.Patch{}, ctx.Err()
	}
}

// DelayValidator is the validating-side equivalent.
type DelayValidator struct {
	Name_ string
	Delay time.Duration
}

func (d *DelayValidator) Name() string { return d.Name_ }
func (d *DelayValidator) Validate(ctx context.Context, _ types.Review) (types.Decision, error) {
	select {
	case <-time.After(d.Delay):
		return types.Decision{Allowed: true}, nil
	case <-ctx.Done():
		return types.Decision{}, ctx.Err()
	}
}

// FlakyMutator fails the first Failures calls with a chosen category, then
// starts emitting its patch. Used to prove retry/backoff in reconciliation.
type FlakyMutator struct {
	Name_        string
	Failures     int
	FailCategory types.Category
	calls        int
	AllowedPaths []string
}

func (f *FlakyMutator) Name() string              { return f.Name_ }
func (f *FlakyMutator) AllowedPrefixes() []string { return f.AllowedPaths }

func (f *FlakyMutator) Mutate(_ context.Context, _ *types.Object) (types.Patch, error) {
	f.calls++
	if f.calls <= f.Failures {
		cat := f.FailCategory
		if cat == "" {
			cat = types.CatComputeFailure
		}
		return types.Patch{}, &admission.PluginError{
			Category: cat, Plugin: f.Name_,
			Message: fmt.Sprintf("synthetic failure %d/%d", f.calls, f.Failures),
		}
	}
	return types.Patch{}, nil
}

// Calls reports how many times the plugin has been invoked (test helper).
func (f *FlakyMutator) Calls() int { return f.calls }

// OscillatingMutator flips an extra value on every call, so the chain can
// never reach a fixed point. The bounded pass loop must turn this into a
// ComputeFailure instead of looping forever.
type OscillatingMutator struct{ calls int }

func (o *OscillatingMutator) Name() string              { return "fixtures.oscillating" }
func (o *OscillatingMutator) AllowedPrefixes() []string { return []string{"/spec/extra/flap"} }

func (o *OscillatingMutator) Mutate(_ context.Context, _ *types.Object) (types.Patch, error) {
	o.calls++
	return types.Patch{Ops: []types.PatchOp{{
		Op: types.OpAdd, Path: "/spec/extra/flap", Value: o.calls,
	}}}, nil
}

// Calls reports invocation count (must be bounded by MaxMutationPasses).
func (o *OscillatingMutator) Calls() int { return o.calls }

// RogueMutator advertises one prefix but patches outside it (and one variant
// attacks the hard-guarded immutable field). Both must be refused with
// IllegalMutation before any change is committed.
type RogueMutator struct {
	Name_  string
	Attack string // "outside" | "immutable"
}

func (r *RogueMutator) Name() string { return r.Name_ }
func (r *RogueMutator) AllowedPrefixes() []string {
	return []string{"/spec/extra/rogue"}
}

func (r *RogueMutator) Mutate(_ context.Context, _ *types.Object) (types.Patch, error) {
	switch r.Attack {
	case "immutable":
		return types.Patch{Ops: []types.PatchOp{{
			Op: types.OpReplace, Path: "/spec/immutable", Value: true,
		}}}, nil
	default:
		return types.Patch{Ops: []types.PatchOp{{
			Op: types.OpReplace, Path: "/spec/replicas", Value: 999,
		}}}, nil
	}
}
