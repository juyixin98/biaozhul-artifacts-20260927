// Package fixture builds deterministic synthetic clusters and requests used by
// demos, the HTTP adapter and tests. Nothing here talks to a real backend; all
// data is generated locally from fixed seeds so results are reproducible.
package fixture

import (
	"encoding/json"
	"fmt"
	"os"

	"opp284/placement/internal/model"
)

// Cluster is the serializable synthetic world.
type Cluster struct {
	Name      string             `json:"name"`
	Nodes     []model.Node       `json:"nodes"`
	Groups    []model.Group      `json:"groups,omitempty"`
	Running   []RunningFixture   `json:"running,omitempty"`
	DeclaredZones []string       `json:"declared_zones,omitempty"`
}

// RunningFixture is a running instance in a fixture world.
type RunningFixture struct {
	ID             string            `json:"id"`
	NodeID         string            `json:"node_id"`
	Request        model.Resources   `json:"request"`
	AffinityGroups []string          `json:"affinity_groups,omitempty"`
}

// Scenario bundles a cluster with the placement request used in a test.
type Scenario struct {
	Description  string                 `json:"description"`
	Cluster      Cluster                `json:"cluster"`
	Intents      []model.Intent         `json:"intents"`
	Replacements map[string]ReplaceSpec `json:"replacements,omitempty"`
	AllowRecreate bool                  `json:"allow_recreate,omitempty"`
	// ExpectFailure, when non-empty, asserts the exact top-level/instance
	// failure codes produced by an independent test oracle.
	ExpectFailure *ExpectFailure `json:"expect_failure,omitempty"`
	// ExpectPlacement asserts instance id -> node id when success expected.
	ExpectPlacement map[string]string `json:"expect_placement,omitempty"`
}

// ReplaceSpec pairs a new intent with the old running instance it replaces.
type ReplaceSpec struct {
	OldID string `json:"old_id"`
}

// ExpectFailure asserts structured failure output.
type ExpectFailure struct {
	TopCode       model.ReasonKind      `json:"top_code"`
	InstanceCodes map[string]model.ReasonKind `json:"instance_codes"`
}

// LoadScenario reads a JSON scenario file.
func LoadScenario(path string) (*Scenario, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read scenario %s: %w", path, err)
	}
	var s Scenario
	if err := json.Unmarshal(b, &s); err != nil {
		return nil, fmt.Errorf("parse scenario %s: %w", path, err)
	}
	return &s, nil
}

// --- Deterministic builders -------------------------------------------------

// BuildSmallCluster returns the canonical 6-node / 3-zone test cluster:
//
//	zones z-a, z-b, z-c; nodes n-1..n-6, 2 nodes per zone.
//	Each node capacity cpu=8,mem=16; n-6 starts cordoned (eligible=false).
//	Labels: role=web on n-1..n-4, role=db on n-5; disk=ssd on n-1,n-3,n-5.
func BuildSmallCluster() Cluster {
	caps := model.Resources{"cpu": 8, "mem": 16}
	mk := func(id, zone string, labels model.Labels, eligible bool) model.Node {
		return model.Node{ID: id, Zone: zone, Capacity: caps.Clone(), Labels: labels, Eligible: eligible}
	}
	return Cluster{
		Name: "small-6n-3z",
		Nodes: []model.Node{
			mk("n-1", "z-a", model.Labels{"role": "web", "disk": "ssd"}, true),
			mk("n-2", "z-a", model.Labels{"role": "web"}, true),
			mk("n-3", "z-b", model.Labels{"role": "web", "disk": "ssd"}, true),
			mk("n-4", "z-b", model.Labels{"role": "web"}, true),
			mk("n-5", "z-c", model.Labels{"role": "db", "disk": "ssd"}, true),
			mk("n-6", "z-c", model.Labels{"role": "web"}, false),
		},
		DeclaredZones: []string{"z-a", "z-b", "z-c"},
	}
}

// Instance is a constructor shortcut.
func Instance(id string, cpu, mem int64, zone string, sel model.Selector, groups ...string) model.Intent {
	return model.Intent{
		ID:             id,
		Request:        model.Resources{"cpu": cpu, "mem": mem},
		RequiredZone:   zone,
		NodeSelector:   sel,
		AffinityGroups: groups,
	}
}

// Group is a constructor shortcut.
func Group(id string, mode model.GroupMode, members ...string) model.Group {
	return model.Group{ID: id, Mode: mode, MemberIDs: members}
}
