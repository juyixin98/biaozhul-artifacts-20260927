package merge

import (
	"encoding/json"

	"fieldmerge/internal/schema"
)

// Input is one declarative apply request as seen by the merge engine.
//
// The engine is pure: it never touches storage, clocks or HTTP. All inputs
// needed for a deterministic three-way merge are supplied explicitly.
type Input struct {
	Kind    string `json:"kind"`
	Name    string `json:"name"`
	Manager string `json:"manager"`
	Force   bool   `json:"force"`
	// Live is the currently merged object (nil on first apply).
	Live json.RawMessage `json:"live,omitempty"`
	// Config is this manager's FULL declaration. Fields absent from Config
	// are retracted by Manager; explicit JSON null means "delete this field"
	// and is distinguishable from absence by map membership.
	Config json.RawMessage `json:"config"`
	// Schema carries list semantics for this Kind.
	Schema *schema.Schema `json:"schema"`
}

// Claim is one ownership record: all managers currently claiming a field path.
type Claim struct {
	Path     string   `json:"path"`
	Managers []string `json:"managers"`
}

// Conflict describes one rejected field path.
type Conflict struct {
	Path   string   `json:"path"`
	Owners []string `json:"owners"`
	Reason string   `json:"reason"`
	Wanted string   `json:"wanted,omitempty"` // intended operation: set/delete/retract/replace
}

// Change is one auditable value/ownership mutation, ordered as produced.
type Change struct {
	Path   string          `json:"path"`
	Op     string          `json:"op"` // set|delete|share|release|takeover
	Reason string          `json:"reason,omitempty"`
	From   json.RawMessage `json:"from,omitempty"`
	To     json.RawMessage `json:"to,omitempty"`
}

// Result is the engine output. When Conflicts is non-empty the caller MUST NOT
// persist Live/Claims; the values are returned only for diagnostics.
type Result struct {
	Kind     string     `json:"kind"`
	Name     string     `json:"name"`
	Live     any        `json:"live"`
	Claims   []Claim    `json:"claims"`
	Changes  []Change   `json:"changes"`
	Conflict []Conflict `json:"conflicts"`
	// PrunedOwnership lists stale claim paths that pointed at fields which
	// no longer exist in Live and were removed (healing on read/write).
	PrunedOwnership []string `json:"pruned_ownership,omitempty"`
}

// cfgVal is a config slot: present distinguishes "not submitted" from an
// explicit JSON null.
type cfgVal struct {
	present bool
	value   any
}

func presentCfg(v any) cfgVal { return cfgVal{present: true, value: v} }

var absentCfg = cfgVal{present: false}

// Reason codes attached to conflicts. Stable strings: tests and clients rely
// on them to classify failures.
const (
	ReasonAtomicMismatch   = "atomic_value_mismatch"
	ReasonExplicitDelete   = "explicit_delete_field_owned_by_other"
	ReasonRetract          = "retract_field_owned_by_other"
	ReasonReplaceSubtree   = "replaces_subtree_with_fields_owned_by_other"
	ReasonRemoveMapElement = "removes_map_element_with_fields_owned_by_other"
)
