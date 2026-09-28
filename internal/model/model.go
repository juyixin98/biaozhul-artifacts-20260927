// Package model holds the cross-package data and error contracts for the
// local infrastructure planner. Every boundary (HTTP -> reconciler ->
// planner -> provider -> journal) speaks in terms of these types.
package model

import (
	"fmt"
	"time"
)

// Kind enumerates the supported synthetic resource types.
type Kind string

const (
	KindVPC      Kind = "vpc"
	KindSubnet   Kind = "subnet"
	KindInstance Kind = "instance"
	KindBucket   Kind = "bucket"
)

// Kinds in dependency-rank order (independent first). The rank is used as a
// tie breaker when topologically sorting resources at the same level, so that
// plans are fully deterministic regardless of map iteration order.
var Kinds = []Kind{KindBucket, KindVPC, KindSubnet, KindInstance}

// Rank returns the topological rank of a kind (lower = earlier to create).
func Rank(k Kind) int {
	for i, kk := range Kinds {
		if kk == k {
			return i
		}
	}
	return len(Kinds)
}

// Ref is a resource reference: a logical name in the desired specification
// is turned into a provider-assigned physical ID at apply time. Keeping
// references name-based means a replace (new physical ID) never forces a
// rewrite of every dependent resource in the declared spec.
type Ref struct {
	Kind Kind   `json:"kind"`
	Name string `json:"name"`
}

func (r Ref) Key() Key { return Key{Kind: r.Kind, Name: r.Name} }

// Key is the stable logical identity of a resource. Logical identity is
// declared by the human; the physical ID is assigned by the provider and
// changes when a resource is replaced.
type Key struct {
	Kind Kind   `json:"kind"`
	Name string `json:"name"`
}

func (k Key) String() string { return string(k.Kind) + "/" + k.Name }

func (k Key) Valid() bool {
	for _, kk := range Kinds {
		if kk == k.Kind {
			return k.Name != ""
		}
	}
	return false
}

// Desired is one resource as declared in the input specification.
//
// Refs lists the resource references this object makes; Attrs holds scalar
// attributes. Immutable attributes (see spec.Schema) are only present in
// Attrs for readability; the planner consults the schema to decide whether a
// change is in-place or requires replacement.
type Desired struct {
	Kind  Kind              `json:"kind"`
	Name  string            `json:"name"`
	Attrs map[string]string `json:"attrs,omitempty"`
	Refs  map[string]Ref    `json:"refs,omitempty"`
	// Protected marks a critical resource. Destroying or replacing it is
	// refused unless the request explicitly releases the guard for it.
	Protected bool `json:"protected,omitempty"`
}

// Key returns the logical key.
func (d Desired) Key() Key { return Key{Kind: d.Kind, Name: d.Name} }

// Attr returns an attribute value and whether it was present.
func (d Desired) Attr(k string) (string, bool) {
	v, ok := d.Attrs[k]
	return v, ok
}

// Live is one resource as observed in the (simulated) real world.
type Live struct {
	Key   Key               `json:"key"`
	ID    string            `json:"id"`
	Attrs map[string]string `json:"attrs,omitempty"`
	Refs  map[string]Ref    `json:"refs,omitempty"`
	// Protected is observed from the real resource, not copied from the
	// spec: a resource protected in the real world stays protected even if
	// the new spec drops the flag.
	Protected bool `json:"protected,omitempty"`
}

// Observation is the full observed state plus a generated timestamp.
type Observation struct {
	Resources  []Live    `json:"resources"`
	ObservedAt time.Time `json:"observed_at"`
}

// ByKey indexes observed resources by logical key.
func (o Observation) ByKey() map[Key]Live {
	m := make(map[Key]Live, len(o.Resources))
	for _, l := range o.Resources {
		m[l.Key] = l
	}
	return m
}

// Failure categories. Every error that crosses a package boundary carries
// exactly one of these codes so tests (and clients) can distinguish input
// errors, state conflicts, resource exhaustion and compute failures.
const (
	// CatInput: the declared specification is malformed.
	CatInput = "input_error"
	// CatConflict: the request conflicts with current state (guard held,
	// drift detected, run not resumable, ...).
	CatConflict = "state_conflict"
	// CatExhaustion: the simulated environment is out of capacity.
	CatExhaustion = "resource_exhaustion"
	// CatCompute: provider/transport/transient compute failure.
	CatCompute = "compute_failure"
)

// Error is the typed error contract shared by every package.
type Error struct {
	Category string // one of Cat*
	Code     string // machine readable sub-code
	Message  string // human readable detail
}

func (e *Error) Error() string { return e.Category + "/" + e.Code + ": " + e.Message }

// E constructs a typed error.
func E(cat, code, format string, args ...any) *Error {
	return &Error{Category: cat, Code: code, Message: fmt.Sprintf(format, args...)}
}

// AsError extracts a *model.Error from any error.
func AsError(err error) (*Error, bool) {
	if err == nil {
		return nil, false
	}
	if me, ok := err.(*Error); ok {
		return me, true
	}
	return nil, false
}

// OpState / RunState are the journal state machines.
type OpState string

const (
	OpPending   OpState = "pending"
	OpInflight  OpState = "inflight"
	OpSucceeded OpState = "succeeded"
	OpFailed    OpState = "failed"
)

type RunState string

const (
	RunPlanning    RunState = "planning"
	RunPlanned     RunState = "planned"
	RunApplying    RunState = "applying"
	RunInterrupted RunState = "interrupted"
	RunSucceeded   RunState = "succeeded"
	RunFailed      RunState = "failed"
)
