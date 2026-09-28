package model

import (
	"errors"
	"fmt"
	"strings"
)

// NodeStatus is the lifecycle state of a node. Only Ready nodes receive
// placements. Disabled nodes model cordoned/draining hosts.
type NodeStatus string

const (
	NodeReady    NodeStatus = "ready"
	NodeDisabled NodeStatus = "disabled"
	NodeNotReady NodeStatus = "not_ready"
)

// TaintEffect defines how a node taint interacts with an instance toleration.
type TaintEffect string

const (
	// TaintNoSchedule is a hard filter: an instance without a matching
	// toleration cannot be placed on the node.
	TaintNoSchedule TaintEffect = "no_schedule"
	// TaintPreferNoSchedule is soft: it only breaks ties toward clean nodes.
	TaintPreferNoSchedule TaintEffect = "prefer_no_schedule"
)

// Taint marks a node so that only instances tolerating it may use it.
type Taint struct {
	Key    string      `json:"key"`
	Value  string      `json:"value"`
	Effect TaintEffect `json:"effect"`
}

// Toleration matches a taint with the same key/value. An empty Value matches
// any value for that key.
type Toleration struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

// Tolerates reports whether the toleration covers the taint.
func (t Toleration) Tolerates(taint Taint) bool {
	if t.Key != taint.Key {
		return false
	}
	return t.Value == "" || t.Value == taint.Value
}

// Node is a candidate placement host.
type Node struct {
	ID       string            `json:"id"`
	Zone     string            `json:"zone"`
	Region   string            `json:"region"`
	Capacity Resources         `json:"capacity"`
	Labels   map[string]string `json:"labels,omitempty"`
	Taints   []Taint           `json:"taints,omitempty"`
	Status   NodeStatus        `json:"status"`
}

// DomainValue extracts the domain identifier for a spread/affinity key.
// The built-in keys "zone" and "region" read the struct fields; any other
// key reads node.Labels[key].
func (n Node) DomainValue(key string) (string, bool) {
	switch key {
	case "zone":
		if n.Zone == "" {
			return "", false
		}
		return n.Zone, true
	case "region":
		if n.Region == "" {
			return "", false
		}
		return n.Region, true
	default:
		v, ok := n.Labels[key]
		return v, ok
	}
}

// Validate checks structural invariants of a node.
func (n *Node) Validate() error {
	var problems []string
	if strings.TrimSpace(n.ID) == "" {
		problems = append(problems, "node id is empty")
	}
	if err := n.Capacity.Validate(); err != nil {
		problems = append(problems, err.Error())
	}
	switch n.Status {
	case NodeReady, NodeDisabled, NodeNotReady:
	default:
		problems = append(problems, fmt.Sprintf("node %q has unknown status %q", n.ID, n.Status))
	}
	for i, t := range n.Taints {
		if t.Key == "" {
			problems = append(problems, fmt.Sprintf("node %q taint[%d] has empty key", n.ID, i))
		}
		switch t.Effect {
		case TaintNoSchedule, TaintPreferNoSchedule:
		default:
			problems = append(problems, fmt.Sprintf("node %q taint[%d] has unknown effect %q", n.ID, i, t.Effect))
		}
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}

// ToleratesAll reports whether the given tolerations cover every hard
// taint. Untolerated soft taints are allowed but reported separately by the
// caller for scoring.
func (n Node) ToleratesAll(tols []Toleration) (hardOK bool, untoleratedSoft int) {
	for _, t := range n.Taints {
		matched := false
		for _, tol := range tols {
			if tol.Tolerates(t) {
				matched = true
				break
			}
		}
		if matched {
			continue
		}
		switch t.Effect {
		case TaintNoSchedule:
			return false, 0
		case TaintPreferNoSchedule:
			untoleratedSoft++
		}
	}
	return true, untoleratedSoft
}
