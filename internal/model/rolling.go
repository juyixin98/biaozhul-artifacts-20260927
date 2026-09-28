package model

// ReplaceRequest asks the scheduler to roll instances over to new
// instance specs (a new generation / image) on the same cluster without
// ever exceeding MaxSurge extra instances or having more than
// MaxUnavailable old instances gone before their replacement lands.
type ReplaceRequest struct {
	RunID string `json:"run_id,omitempty"`
	Nodes []Node `json:"nodes"`
	// Old are currently bound instances, all of which must be replaced.
	Old []Instance `json:"old"`
	// New are the replacement instances, same cardinality and IDs pair by
	// Replaces.
	New []Instance `json:"new"`
	// Replaces maps new instance ID -> old instance ID.
	Replaces map[string]string `json:"replaces"`
	Policy   Policy            `json:"policy"`
	Options  PlanOptions       `json:"options,omitempty"`
	// MaxSurge is the maximum number of new instances that may exist while
	// their old counterpart is still bound.
	MaxSurge int `json:"max_surge"`
	// MaxUnavailable is the maximum number of old instances that may be
	// evicted before their replacement is bound.
	MaxUnavailable int `json:"max_unavailable"`
}

// ReplaceOp is one step in the rolling order.
type ReplaceOp struct {
	Kind       string `json:"kind"` // evict_old | place_new
	InstanceID string `json:"instance_id"`
	NodeID     string `json:"node_id,omitempty"` // place_new only
}

// ReplaceResult reports either a feasible evict/place order or conflicts.
type ReplaceResult struct {
	RunID     string      `json:"run_id"`
	Feasible  bool        `json:"feasible"`
	Ops       []ReplaceOp `json:"ops,omitempty"`
	Final     []Decision  `json:"final_placement,omitempty"`
	Conflicts []Conflict  `json:"conflicts,omitempty"`
	Trace     []TraceStep `json:"trace,omitempty"`
	Objective *Objective  `json:"objective,omitempty"`
}
