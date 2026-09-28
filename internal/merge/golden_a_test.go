package merge_test

import (
	"testing"

	"fieldmerge/internal/merge"
)

// TestThreeWay_A_TwoManagersConflictAndForce is the headline scenario:
//
//	Two managers own DISJOINT fields. Each can freely change its own. A
//	non-force cross-write is rejected with the original owner in the conflict
//	record; the same write with force takes the field over. The foreign
//	manager's unrelated fields survive every step.
//
// All "want" literals were derived by hand (the manual three-way table is in
// testdata/threeway/scenario_A.md). The engine never generates them.
func TestThreeWay_A_TwoManagersConflictAndForce(t *testing.T) {
	sc := widgetSchema(t)

	steps := []goldenStep{
		{
			name:    "net establishes its fields: image and the edge-1 ingress",
			manager: "net",
			config: `{
			  "image": "registry/widget:1",
			  "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80}]
			}`,
			wantLive: `{
			  "image": "registry/widget:1",
			  "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80}]
			}`,
			wantOwners: map[string][]string{
				"image":                         {"net"},
				`ingresses[name="edge-1"].host`: {"net"},
				`ingresses[name="edge-1"].port`: {"net"},
			},
			rationale: "net claims image and edge-1's leaves",
		},
		{
			name:    "sre establishes its disjoint field replicas (unowned -> takes it cleanly)",
			manager: "sre",
			config:  `{"replicas": 3}`,
			wantLive: `{
			  "replicas": 3,
			  "image": "registry/widget:1",
			  "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80}]
			}`,
			wantOwners: map[string][]string{
				"replicas":                      {"sre"},
				"image":                         {"net"},
				`ingresses[name="edge-1"].host`: {"net"},
				`ingresses[name="edge-1"].port`: {"net"},
			},
			wantChangesOps: map[string][]string{
				"replicas": {"set"},
			},
			rationale: "disjoint ownership from birth; net's fields untouched",
		},
		{
			name:    "sre changes its own replicas 3 -> 5",
			manager: "sre",
			config:  `{"replicas": 5}`,
			wantLive: `{
			  "replicas": 5,
			  "image": "registry/widget:1",
			  "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80}]
			}`,
			wantOwners: map[string][]string{
				"replicas":                      {"sre"},
				"image":                         {"net"},
				`ingresses[name="edge-1"].host`: {"net"},
				`ingresses[name="edge-1"].port`: {"net"},
			},
			wantChangesOps: map[string][]string{
				"replicas": {"set"},
			},
			rationale: "different-field writes never touch image/ingresses",
		},
		{
			name:    "sre writes net's field image WITHOUT force -> conflict, no preemption",
			manager: "sre",
			config:  `{"replicas": 5, "image": "registry/widget:2"}`,
			expectConflicts: map[string]string{
				"image": merge.ReasonAtomicMismatch,
			},
			rationale: "default apply never preempts; conflict returns path and owner=net",
		},
		{
			name:    "sre retakes image WITH force; net's ingress stays intact",
			manager: "sre",
			force:   true,
			config:  `{"replicas": 5, "image": "registry/widget:2"}`,
			wantLive: `{
			  "replicas": 5,
			  "image": "registry/widget:2",
			  "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80}]
			}`,
			wantOwners: map[string][]string{
				"replicas":                      {"sre"},
				"image":                         {"sre"},
				`ingresses[name="edge-1"].host`: {"net"},
				`ingresses[name="edge-1"].port`: {"net"},
			},
			wantChangesOps: map[string][]string{
				"image": {"takeover"},
			},
			rationale: "explicit force transfers only image; edge-1 host/port still owned by net",
		},
		{
			name:    "net reapplies its declaration; image now conflicts (lost to sre)",
			manager: "net",
			config: `{
			  "image": "registry/widget:1",
			  "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80}]
			}`,
			expectConflicts: map[string]string{
				"image": merge.ReasonAtomicMismatch,
			},
			rationale: "response names the stolen field with sre; replicas absent from config is untouched",
		},
	}
	runGolden(t, sc, steps)
}
