package scheduler

import (
	"fmt"
	"strings"

	"placer/internal/model"
)

// validateReplace validates a rolling request structurally and
// semantically. Old and new instances live in the same occupancy world
// during the search, so their id spaces must be disjoint; callers
// conventionally suffix new-generation ids with "-v2".
func validateReplace(req model.ReplaceRequest) error {
	var problems []string
	if len(req.Old) == 0 {
		problems = append(problems, "old instances list is empty")
	}
	if len(req.New) == 0 {
		problems = append(problems, "new instances list is empty")
	}
	if len(req.Old) != len(req.New) {
		problems = append(problems, fmt.Sprintf("old/new cardinality mismatch: %d vs %d", len(req.Old), len(req.New)))
	}
	if req.MaxSurge < 0 {
		problems = append(problems, "max_surge must be >= 0")
	}
	if req.MaxUnavailable < 0 {
		problems = append(problems, "max_unavailable must be >= 0")
	}
	if req.MaxSurge == 0 && req.MaxUnavailable == 0 {
		problems = append(problems, "max_surge and max_unavailable cannot both be 0 (no first move possible)")
	}
	if err := req.Policy.Validate(); err != nil {
		problems = append(problems, err.Error())
	}

	nodeIDs := map[string]bool{}
	for _, n := range req.Nodes {
		if err := n.Validate(); err != nil {
			problems = append(problems, err.Error())
		}
		nodeIDs[n.ID] = true
	}

	oldIDs := map[string]bool{}
	for _, o := range req.Old {
		if err := o.Validate(); err != nil {
			problems = append(problems, err.Error())
		}
		if o.State != model.StateBound && o.State != "" {
			problems = append(problems, fmt.Sprintf("old instance %q must be bound, got %s", o.ID, o.State))
		}
		if o.NodeID == "" {
			problems = append(problems, "old instance "+o.ID+" has no node_id")
		} else if !nodeIDs[o.NodeID] {
			problems = append(problems, fmt.Sprintf("old instance %q bound to unknown node %q", o.ID, o.NodeID))
		}
		if oldIDs[o.ID] {
			problems = append(problems, "duplicate old instance id "+o.ID)
		}
		oldIDs[o.ID] = true
	}

	newIDs := map[string]bool{}
	for _, n := range req.New {
		if err := n.Validate(); err != nil {
			problems = append(problems, err.Error())
		}
		if oldIDs[n.ID] {
			problems = append(problems, "new instance id "+n.ID+" collides with an old instance id (use e.g. a -v2 suffix)")
		}
		if newIDs[n.ID] {
			problems = append(problems, "duplicate new instance id "+n.ID)
		}
		newIDs[n.ID] = true
	}

	if req.Replaces == nil && len(req.Old) > 0 {
		problems = append(problems, "replaces mapping is required")
	}
	targeted := map[string]bool{}
	for newID, oldID := range req.Replaces {
		if !newIDs[newID] {
			problems = append(problems, fmt.Sprintf("replaces maps unknown new instance %q", newID))
		}
		if !oldIDs[oldID] {
			problems = append(problems, fmt.Sprintf("replaces[%q] targets unknown old instance %q", newID, oldID))
		}
		targeted[oldID] = true
	}
	for id := range oldIDs {
		if !targeted[id] {
			problems = append(problems, "old instance "+id+" has no replacement mapping")
		}
	}

	if len(problems) > 0 {
		return fmt.Errorf("replace validation failed: %s", strings.Join(problems, "; "))
	}
	return nil
}
