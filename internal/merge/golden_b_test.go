package merge_test

import (
	"testing"

	"fieldmerge/internal/merge"
)

// TestThreeWay_B_NestedLists exercises all three declared list semantics
// nested in one resource:
//
//	ingresses = map list keyed by "name"  (per-element-field ownership)
//	tags      = set of scalars            (per-value ownership)
//	servers   = atomic list               (whole-list ownership)
//
// It verifies nested-leaf conflicts, explicit force reaching inside an
// element, that omitting a FOREIGN set value is not a deletion, and that
// unrelated data from another manager never disappears.
//
// Each declaration is the manager's FULL statement (like real server-side
// apply): managers re-declare everything they own so the hand-derived merge
// stays unambiguous. All expected literals were computed by hand.
func TestThreeWay_B_NestedLists(t *testing.T) {
	sc := widgetSchema(t)

	steps := []goldenStep{
		{
			name:    "net creates keyed element, set value and atomic list",
			manager: "net",
			config: `{
			  "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80, "tls": true}],
			  "tags": ["canary"],
			  "servers": [{"zone": "z1", "weight": 10}]
			}`,
			wantLive: `{
			  "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80, "tls": true}],
			  "tags": ["canary"],
			  "servers": [{"zone": "z1", "weight": 10}]
			}`,
			wantOwners: map[string][]string{
				`ingresses[name="edge-1"].host`: {"net"},
				`ingresses[name="edge-1"].port`: {"net"},
				`ingresses[name="edge-1"].tls`:  {"net"},
				`tags[^"canary"]`:               {"net"},
				"servers":                       {"net"},
			},
			rationale: "map leaves claimed individually; set value by token; atomic list as one path",
		},
		{
			name:    "web adds element edge-2 and tag beta; nothing of net's changes",
			manager: "web",
			config: `{
			  "ingresses": [{"name": "edge-2", "host": "b.example", "port": 443}],
			  "tags": ["beta"]
			}`,
			wantLive: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a.example", "port": 80, "tls": true},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["beta", "canary"],
			  "servers": [{"zone": "z1", "weight": 10}]
			}`,
			wantOwners: map[string][]string{
				`ingresses[name="edge-1"].host`: {"net"},
				`ingresses[name="edge-1"].port`: {"net"},
				`ingresses[name="edge-1"].tls`:  {"net"},
				`ingresses[name="edge-2"].host`: {"web"},
				`ingresses[name="edge-2"].port`: {"web"},
				`tags[^"canary"]`:               {"net"},
				`tags[^"beta"]`:                 {"web"},
				"servers":                       {"net"},
			},
			rationale: "edge-2 appended in order; edge-1/canary/servers preserved untouched",
		},
		{
			name:    "web changes host INSIDE net's element (keeping its own edge-2) -> conflict",
			manager: "web",
			config: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a-hijack.example"},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["beta"]
			}`,
			expectConflicts: map[string]string{
				`ingresses[name="edge-1"].host`: merge.ReasonAtomicMismatch,
			},
			rationale: "conflict path is the nested leaf and the reported owner is net; whole apply rejected",
		},
		{
			name:    "web forces the nested host takeover; edge-2 and all net fields survive",
			manager: "web",
			force:   true,
			config: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a-hijack.example"},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["beta"]
			}`,
			wantLive: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a-hijack.example", "port": 80, "tls": true},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["beta", "canary"],
			  "servers": [{"zone": "z1", "weight": 10}]
			}`,
			wantOwners: map[string][]string{
				`ingresses[name="edge-1"].host`: {"web"},
				`ingresses[name="edge-1"].port`: {"net"},
				`ingresses[name="edge-1"].tls`:  {"net"},
				`ingresses[name="edge-2"].host`: {"web"},
				`ingresses[name="edge-2"].port`: {"web"},
				`tags[^"canary"]`:               {"net"},
				`tags[^"beta"]`:                 {"web"},
				"servers":                       {"net"},
			},
			wantChangesOps: map[string][]string{
				`ingresses[name="edge-1"].host`: {"takeover"},
			},
			rationale: "only edge-1.host flips; port/tls remain net's; edge-2/canary/servers untouched",
		},
		{
			name:    "net submits its full declaration without canary: own value retracted, beta stays",
			manager: "net",
			config: `{
			  "ingresses": [{"name": "edge-1", "port": 80, "tls": true}],
			  "tags": [],
			  "servers": [{"zone": "z1", "weight": 10}]
			}`,
			wantLive: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a-hijack.example", "port": 80, "tls": true},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["beta"],
			  "servers": [{"zone": "z1", "weight": 10}]
			}`,
			wantOwners: map[string][]string{
				`ingresses[name="edge-1"].host`: {"web"},
				`ingresses[name="edge-1"].port`: {"net"},
				`ingresses[name="edge-1"].tls`:  {"net"},
				`ingresses[name="edge-2"].host`: {"web"},
				`ingresses[name="edge-2"].port`: {"web"},
				`tags[^"beta"]`:                 {"web"},
				"servers":                       {"net"},
			},
			wantChangesOps: map[string][]string{
				`tags[^"canary"]`: {"delete"},
			},
			rationale: "omitting canary (net owns it) retracts it; edge-1.host now web's is preserved",
		},
		{
			name:    "net re-declares canary while omitting beta: beta (web) must survive omission",
			manager: "net",
			config: `{
			  "ingresses": [{"name": "edge-1", "port": 80, "tls": true}],
			  "tags": ["canary"],
			  "servers": [{"zone": "z1", "weight": 10}]
			}`,
			wantLive: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a-hijack.example", "port": 80, "tls": true},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["canary", "beta"],
			  "servers": [{"zone": "z1", "weight": 10}]
			}`,
			wantOwners: map[string][]string{
				`ingresses[name="edge-1"].host`: {"web"},
				`ingresses[name="edge-1"].port`: {"net"},
				`ingresses[name="edge-1"].tls`:  {"net"},
				`ingresses[name="edge-2"].host`: {"web"},
				`ingresses[name="edge-2"].port`: {"web"},
				`tags[^"beta"]`:                 {"web"},
				`tags[^"canary"]`:               {"net"},
				"servers":                       {"net"},
			},
			rationale: "omission of a foreign set value is NOT a deletion and raises NO conflict",
		},
		{
			name:    "web attempts net's atomic servers list while restating all its own fields -> conflict",
			manager: "web",
			config: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a-hijack.example"},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["beta"],
			  "servers": [{"zone": "z9", "weight": 99}]
			}`,
			expectConflicts: map[string]string{
				"servers": merge.ReasonAtomicMismatch,
			},
			rationale: "atomic list is one opaque field owned by net; nothing is committed",
		},
		{
			name:    "web forces the atomic servers replacement; only servers changes",
			manager: "web",
			force:   true,
			config: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a-hijack.example"},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["beta"],
			  "servers": [{"zone": "z9", "weight": 99}]
			}`,
			wantLive: `{
			  "ingresses": [
			    {"name": "edge-1", "host": "a-hijack.example", "port": 80, "tls": true},
			    {"name": "edge-2", "host": "b.example", "port": 443}
			  ],
			  "tags": ["beta", "canary"],
			  "servers": [{"zone": "z9", "weight": 99}]
			}`,
			wantOwners: map[string][]string{
				`ingresses[name="edge-1"].host`: {"web"},
				`ingresses[name="edge-1"].port`: {"net"},
				`ingresses[name="edge-1"].tls`:  {"net"},
				`ingresses[name="edge-2"].host`: {"web"},
				`ingresses[name="edge-2"].port`: {"web"},
				`tags[^"beta"]`:                 {"web"},
				`tags[^"canary"]`:               {"net"},
				"servers":                       {"web"},
			},
			wantChangesOps: map[string][]string{
				"servers": {"takeover"},
			},
			rationale: "whole-list ownership transfers to web; every other field unchanged",
		},
	}
	runGolden(t, sc, steps)
}
