package model

import (
	"errors"
	"fmt"
	"strings"
)

// PolicyMode distinguishes hard constraints (filters) from soft preferences
// (score contributions).
type PolicyMode string

const (
	ModeHard PolicyMode = "hard"
	ModeSoft PolicyMode = "soft"
)

// GroupRule is a cluster-level rule for one group namespace, e.g. group
// "app" with anti_affinity on domain "zone" spreads instances sharing the
// same app group value across different zones.
type GroupRule struct {
	// Group is the membership key matched against Instance.Groups.
	Group string `json:"group"`
	// Mode hard => illegal placement, soft => score only.
	Mode PolicyMode `json:"mode"`
	// Affinity: instances of the same group value prefer/require the same
	// domain. AntiAffinity (false): they prefer/require distinct domains.
	Affinity bool `json:"affinity"`
	// TopologyKey defines the domain ("zone", "region" or a node label key).
	TopologyKey string `json:"topology_key"`
}

func (r GroupRule) kind() string {
	if r.Affinity {
		return "affinity"
	}
	return "anti_affinity"
}

// Policy holds all scheduler-level (as opposed to per-instance) rules.
type Policy struct {
	Groups []GroupRule `json:"groups,omitempty"`
}

// Validate rejects unknown modes and empty keys.
func (p Policy) Validate() error {
	var problems []string
	seen := map[string]bool{}
	for i, r := range p.Groups {
		switch r.Mode {
		case ModeHard, ModeSoft:
		default:
			problems = append(problems, fmt.Sprintf("policy.groups[%d] (%s/%s) has unknown mode %q",
				i, r.Group, r.kind(), r.Mode))
		}
		if strings.TrimSpace(r.Group) == "" {
			problems = append(problems, fmt.Sprintf("policy.groups[%d] has empty group key", i))
		}
		if strings.TrimSpace(r.TopologyKey) == "" {
			problems = append(problems, fmt.Sprintf("policy.groups[%d] (%s) has empty topology key", i, r.Group))
		}
		// Two rules on the same group+topology may coexist when they are
		// distinct (one affinity and one anti-affinity, or one hard and one
		// soft); identical duplicates are rejected.
		key := r.Group + "|" + r.TopologyKey + "|" + r.kind() + "|" + string(r.Mode)
		if seen[key] {
			problems = append(problems, fmt.Sprintf("policy.groups[%d] duplicates rule %s/%s on %s (%s)",
				i, r.kind(), r.Mode, r.TopologyKey, r.Group))
		}
		seen[key] = true
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}
