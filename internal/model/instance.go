package model

import (
	"errors"
	"strings"
)

// InstanceState is the lifecycle state of a compute instance.
type InstanceState string

const (
	// StatePending instances are unbound and waiting for a placement.
	StatePending InstanceState = "pending"
	// StateBound instances occupy a node and consume its resources.
	StateBound InstanceState = "bound"
	// StateEvicted instances have been removed during a rolling replacement.
	StateEvicted InstanceState = "evicted"
	// StateFailed instances exhausted scheduling retries in the reconcile
	// loop and require human intervention.
	StateFailed InstanceState = "failed"
)

// Instance is a compute instance requesting placement.
type Instance struct {
	ID           string            `json:"id"`
	Request      Resources         `json:"request"`
	Zone         string            `json:"zone,omitempty"` // empty = any zone
	NodeSelector map[string]string `json:"node_selector,omitempty"`
	Tolerations  []Toleration      `json:"tolerations,omitempty"`
	// Groups are group memberships used by scheduler-level affinity
	// policies, e.g. {"app": "checkout", "tenant": "t1"}.
	Groups map[string]string `json:"groups,omitempty"`
	// NodeID is set for bound instances and empty for pending ones.
	NodeID string        `json:"node_id,omitempty"`
	State  InstanceState `json:"state"`
	// Attempts counts scheduling passes that failed for this instance.
	Attempts int `json:"attempts,omitempty"`
	// LastCode is the most recent rejection category, if any.
	LastCode string `json:"last_code,omitempty"`
}

// Validate checks structural invariants.
func (i *Instance) Validate() error {
	var problems []string
	if strings.TrimSpace(i.ID) == "" {
		problems = append(problems, "instance id is empty")
	}
	if err := i.Request.Validate(); err != nil {
		problems = append(problems, err.Error())
	}
	switch i.State {
	case StatePending, StateBound, StateEvicted, StateFailed:
	default:
		problems = append(problems, "instance "+i.ID+" has unknown state "+string(i.State))
	}
	if i.State == StateBound && i.NodeID == "" {
		problems = append(problems, "bound instance "+i.ID+" has no node_id")
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}
