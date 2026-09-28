package model

// RejectCode classifies the reason a single (instance, node) pair fails a
// hard filter, or an instance fails on every node. Codes are stable strings
// so tests can assert a specific failure category.
type RejectCode string

const (
	RejectZoneMismatch      RejectCode = "zone_mismatch"
	RejectNodeNotReady      RejectCode = "node_not_ready"
	RejectNodeSelector      RejectCode = "node_selector_unmatched"
	RejectTaintNotTolerated RejectCode = "taint_not_tolerated"
	RejectResources         RejectCode = "insufficient_resources"
	RejectAntiAffinity      RejectCode = "anti_affinity_conflict"
	RejectAffinity          RejectCode = "affinity_unfulfillable"
	RejectDomainMissing     RejectCode = "domain_missing"
	RejectNone              RejectCode = "" // candidate accepted
)

// Rejection records why a node was eliminated for an instance during the
// hard-filter phase.
type Rejection struct {
	InstanceID string     `json:"instance_id"`
	NodeID     string     `json:"node_id"`
	Code       RejectCode `json:"code"`
	Detail     string     `json:"detail,omitempty"`
}

// Conflict describes an instance that cannot be placed at all, including the
// category used by callers to decide remediation.
type Conflict struct {
	InstanceID string     `json:"instance_id"`
	Code       RejectCode `json:"code"`
	Detail     string     `json:"detail"`
	// BlockedBy names existing/pending instances responsible when the
	// category is a group conflict.
	BlockedBy []string `json:"blocked_by,omitempty"`
}

// SkewSnapshot reports the domain distribution used for a decision so the
// reason can be audited after the fact.
type SkewSnapshot struct {
	TopologyKey string         `json:"topology_key"`
	Mode        string         `json:"mode"` // configured | eligible
	Loads       map[string]int `json:"loads"`
	// CountedDomains are the domains included in the skew computation;
	// absent domains (no node / no eligible node) are listed separately in
	// ExcludedDomains so the counting policy is never implicit.
	CountedDomains  []string `json:"counted_domains"`
	ExcludedDomains []string `json:"excluded_domains,omitempty"`
	MaxLoad         int      `json:"max_load"`
	MinLoad         int      `json:"min_load"`
	Skew            int      `json:"skew"`
}

// Decision binds one instance to one node.
type Decision struct {
	InstanceID string `json:"instance_id"`
	NodeID     string `json:"node_id"`
}

// TraceStep is one auditable scheduler step. For the exact solver a step is
// either a filter (rejections) or a complete assignment evaluation; for the
// greedy solver it is one instance decision with node scores.
type TraceStep struct {
	Kind         string               `json:"kind"` // filter | greedy_pick | assignment_considered | final
	InstanceID   string               `json:"instance_id,omitempty"`
	NodeID       string               `json:"node_id,omitempty"`
	Accepted     *bool                `json:"accepted,omitempty"`
	Code         RejectCode           `json:"code,omitempty"`
	Detail       string               `json:"detail,omitempty"`
	CandidateID  string               `json:"candidate_id,omitempty"`
	NodeScores   map[string]NodeScore `json:"node_scores,omitempty"`
	Skew         *SkewSnapshot        `json:"skew,omitempty"`
	Objective    *Objective           `json:"objective,omitempty"`
	ImprovesBest bool                 `json:"improves_best,omitempty"`
}

// NodeScore is the score tuple for one candidate node. Smaller is better;
// the fields are compared lexicographically in this order.
type NodeScore struct {
	Skew       int `json:"skew"`
	SumSquares int `json:"sum_squares"`
	SoftGroups int `json:"soft_group_violations"`
}

// Objective is the global objective tuple of a complete assignment.
type Objective struct {
	Skew       int `json:"skew"`
	SumSquares int `json:"sum_squares"`
	SoftGroups int `json:"soft_group_violations"`
}

// PlanRequest is the input to a batch placement.
type PlanRequest struct {
	RunID     string      `json:"run_id,omitempty"`
	Nodes     []Node      `json:"nodes"`
	Instances []Instance  `json:"instances"` // pending instances to place
	Bound     []Binding   `json:"bound,omitempty"`
	Policy    Policy      `json:"policy"`
	Options   PlanOptions `json:"options,omitempty"`
}

// Binding is an already-placed instance occupying a node.
type Binding struct {
	InstanceID string            `json:"instance_id"`
	NodeID     string            `json:"node_id"`
	Request    Resources         `json:"request"`
	Groups     map[string]string `json:"groups,omitempty"`
}

// PlanOptions configures the placement semantics.
type PlanOptions struct {
	// SpreadTopologyKey is the distribution domain; default "zone".
	SpreadTopologyKey string `json:"spread_topology_key,omitempty"`
	// IncludeEmptyDomains includes domains with zero bound load as long as
	// they contain a ready candidate-eligible node. Default true; use a
	// pointer so that an explicitly supplied false is distinguishable from
	// an omitted field.
	IncludeEmptyDomains *bool `json:"include_empty_domains,omitempty"`
	// SkewDomainMode:
	//   "configured" (default) — count every domain present on any node in
	//     the cluster config, including nodes ineligible for this batch;
	//   "eligible"             — count only domains containing a node that
	//     survives status/selector/taint (but not resource) pre-filtering.
	SkewDomainMode string `json:"skew_domain_mode,omitempty"`
	// MaxNodesForExact forces the greedy path above this many pending
	// instances; 0 uses the package default.
	MaxNodesForExact int `json:"max_nodes_for_exact,omitempty"`
	// SearchBudget caps exact-solver node visits; 0 uses the package default.
	SearchBudget int `json:"search_budget,omitempty"`
}

// PlanResult is the output of a batch placement. Feasible is false exactly
// when Conflicts is non-empty; in that case Decisions is empty and nothing
// is placed — soft scores never rescue an illegal node.
type PlanResult struct {
	RunID     string      `json:"run_id"`
	Feasible  bool        `json:"feasible"`
	Decisions []Decision  `json:"decisions,omitempty"`
	Conflicts []Conflict  `json:"conflicts,omitempty"`
	Trace     []TraceStep `json:"trace,omitempty"`
	Objective *Objective  `json:"objective,omitempty"`
	Solver    string      `json:"solver"` // exact | greedy
}
