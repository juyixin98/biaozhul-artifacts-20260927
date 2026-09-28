// Package model defines the resource model for the compute-instance node
// placement backend: nodes, instances, resource vectors, labels, domains
// (zones), placement intents and solved plans.
//
// The types in this package carry no scheduling logic beyond basic field
// validation; hard/soft constraint evaluation lives in internal/scheduler.
package model

import (
	"errors"
	"fmt"
	"sort"
	"strings"
)

// Resources is a multi-dimensional resource vector. Unknown keys are valid
// (plugins may introduce "gpu" etc.). Every value must be non-negative.
type Resources map[string]int64

// Clone returns a deep copy.
func (r Resources) Clone() Resources {
	c := make(Resources, len(r))
	for k, v := range r {
		c[k] = v
	}
	return c
}

// Add returns r+x as a new vector.
func (r Resources) Add(x Resources) Resources {
	c := r.Clone()
	for k, v := range x {
		c[k] += v
	}
	return c
}

// Sub returns r-x as a new vector.
func (r Resources) Sub(x Resources) Resources {
	c := r.Clone()
	for k, v := range x {
		c[k] -= v
	}
	return c
}

// Fits reports whether every dimension of r is >= need. Keys present only in
// need are treated as 0 on the capacity side, so a request for a resource the
// node does not advertise never fits (capacity 0, need > 0).
func (r Resources) Fits(need Resources) bool {
	for k, v := range need {
		if r[k] < v {
			return false
		}
	}
	return true
}

// Keys returns the sorted union of dimension names.
func (r Resources) Keys() []string {
	seen := map[string]bool{}
	for k := range r {
		seen[k] = true
	}
	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func (r Resources) validateNonNeg() error {
	for k, v := range r {
		if v < 0 {
			return fmt.Errorf("resource %q must be non-negative, got %d", k, v)
		}
	}
	return nil
}

// Node is a candidate placement target.
type Node struct {
	ID        string    `json:"id"`
	Zone      string    `json:"zone"` // domain identifier; "" is invalid
	Capacity  Resources `json:"capacity"`
	Used      Resources `json:"used"` // already-consumed capacity by running workloads
	Labels    Labels    `json:"labels"`
	// Eligible=false removes the node from consideration for every instance
	// (maintenance, cordoned, hardware fault). Such nodes still exist in the
	// model — this distinction matters for skew domain bookkeeping.
	Eligible  bool      `json:"eligible"`
}

// Free returns the remaining capacity vector.
func (n Node) Free() Resources {
	return n.Capacity.Sub(n.Used)
}

// Validate checks structural invariants of a node.
func (n Node) Validate() error {
	var problems []string
	if n.ID == "" {
		problems = append(problems, "node id is empty")
	}
	if n.Zone == "" {
		problems = append(problems, "node " + n.ID + " has empty zone")
	}
	if err := n.Capacity.validateNonNeg(); err != nil {
		problems = append(problems, err.Error())
	}
	if err := n.Used.validateNonNeg(); err != nil {
		problems = append(problems, err.Error())
	}
	for k, v := range n.Used {
		if v > n.Capacity[k] {
			problems = append(problems, fmt.Sprintf("node %s used %s=%d exceeds capacity %d", n.ID, k, v, n.Capacity[k]))
		}
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}

// Labels is a flat key/value metadata map on nodes and instances.
type Labels map[string]string

// Selector is a conjunction of label requirements: every Key in Equal must
// match, and every Key in NotEqual must not take the given value (absence of
// the key satisfies NotEqual, matching Kubernetes semantics).
type Selector struct {
	Equal    map[string]string `json:"equal,omitempty"`
	NotEqual map[string]string `json:"not_equal,omitempty"`
}

// Matches reports whether labels satisfy the selector.
func (s Selector) Matches(l Labels) bool {
	for k, v := range s.Equal {
		if l[k] != v {
			return false
		}
	}
	for k, v := range s.NotEqual {
		if l[k] == v {
			return false
		}
	}
	return true
}

// GroupMode enumerates how an affinity group constrains placement.
type GroupMode string

// Supported group modes.
const (
	// GroupAffinity: all instances of the group must end up on the SAME node.
	GroupAffinity GroupMode = "affinity"
	// GroupAntiAffinity: all instances of the group must end up on DIFFERENT
	// nodes. Zonal (failure-domain) anti-affinity is expressed separately via
	// AntiAffinityZones.
	GroupAntiAffinity GroupMode = "anti_affinity"
)

// Group is a named constraint over a set of instance ids.
type Group struct {
	ID        string    `json:"id"`
	Mode      GroupMode `json:"mode"`
	MemberIDs []string  `json:"member_ids"`
}

// Intent carries one instance's scheduling requirements.
type Intent struct {
	ID        string    `json:"id"`
	Request   Resources `json:"request"`
	// RequiredZone, when non-empty, pins the instance to that zone.
	RequiredZone string   `json:"required_zone,omitempty"`
	NodeSelector Selector `json:"node_selector,omitempty"`
	// AffinityGroups lists group ids this instance belongs to.
	AffinityGroups []string `json:"affinity_groups,omitempty"`
}

// Validate checks structural invariants of an intent.
func (i Intent) Validate() error {
	var problems []string
	if i.ID == "" {
		problems = append(problems, "instance id is empty")
	}
	if err := i.Request.validateNonNeg(); err != nil {
		problems = append(problems, err.Error())
	}
	if len(i.Request) == 0 {
		problems = append(problems, "instance "+i.ID+" declares no resource request")
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}

// Placement is one resolved instance->node assignment.
type Placement struct {
	InstanceID string `json:"instance_id"`
	NodeID     string `json:"node_id"`
	Zone       string `json:"zone"`
	// ReplacesID is set when this placement supersedes a running instance
	// (rolling replacement); empty for new placements.
	ReplacesID string `json:"replaces_id,omitempty"`
}

// ReasonKind enumerates structured failure categories. The API must never
// collapse unknown/error states into a generic success.
type ReasonKind string

// Failure categories returned in PlanFailure and per-instance reasons.
const (
	ReasonMissingDomain        ReasonKind = "MISSING_DOMAIN"        // pinned zone does not exist in the cluster
	ReasonDomainNoEligibleNode ReasonKind = "DOMAIN_NO_ELIGIBLE_NODE" // zone exists but every node is ineligible
	ReasonNoEligibleNode       ReasonKind = "NO_ELIGIBLE_NODE"      // every candidate node is cordoned
	ReasonSelectorNoMatch      ReasonKind = "SELECTOR_NO_MATCH"     // no node satisfies the label selector
	ReasonZoneMismatch         ReasonKind = "ZONE_MISMATCH"         // only pinned-zone nodes would be legal, none usable
	ReasonInsufficientResource ReasonKind = "INSUFFICIENT_RESOURCE" // node-state/label/zone are fine but free capacity is not
	ReasonAffinityConflict     ReasonKind = "AFFINITY_CONFLICT"     // co-located group members cannot share a node
	ReasonAntiAffinityConflict ReasonKind = "ANTI_AFFINITY_CONFLICT" // group members forced onto one node
	ReasonRollingBlocked       ReasonKind = "ROLLING_REPLACEMENT_BLOCKED" // replacement impossible without evicting first
	ReasonNoNodesInCluster     ReasonKind = "NO_NODES_IN_CLUSTER"
	ReasonEmptyRequest         ReasonKind = "EMPTY_REQUEST"
	// ReasonConstraintConflict is the top-level code when one or more
	// instances fail hard constraints; per-instance codes carry the detail.
	ReasonConstraintConflict   ReasonKind = "CONSTRAINT_CONFLICT"
	ReasonInternal             ReasonKind = "INTERNAL"
)

// HardReject describes why a single (instance, node) pair failed a HARD
// constraint. Rejects are collected during filtering; they explain decisions
// and feed per-instance failure categorization.
type HardReject struct {
	Code   ReasonKind `json:"code"`
	NodeID string     `json:"node_id"`
	Detail string     `json:"detail"`
}

func (h HardReject) String() string {
	if h.Detail == "" {
		return string(h.Code) + "@" + h.NodeID
	}
	return string(h.Code) + "@" + h.NodeID + ": " + h.Detail
}

// PlanFailure is returned (status 409 for constraint conflicts) when no legal
// assignment exists. It names each unschedulable instance with concrete
// reasons instead of falling back to an arbitrary placement.
type PlanFailure struct {
	Code      ReasonKind                 `json:"code"` // top-level category
	Message   string                     `json:"message"`
	Instances []InstanceFailure          `json:"instances,omitempty"`
}

// InstanceFailure explains one unschedulable instance.
type InstanceFailure struct {
	InstanceID string       `json:"instance_id"`
	Reasons    []HardReject `json:"reasons"`
}

// Error implements error.
func (f *PlanFailure) Error() string {
	return "placement failed: " + string(f.Code) + ": " + f.Message
}
