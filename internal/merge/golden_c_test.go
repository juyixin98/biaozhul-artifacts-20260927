package merge_test

import (
	"testing"

	"fieldmerge/internal/merge"
)

// TestThreeWay_C_UnsetVsExplicitDelete pins down the distinction the task
// calls out: a field ABSENT from a manager's declaration ("unsubmitted") is
// not the same as a field present with explicit JSON null ("delete me").
//
//   - omitting a field you own        -> it is retracted (deleted)
//   - omitting a field others own     -> untouched
//   - explicit null on an owned field -> deleted + audited as explicit
//   - explicit null on a foreign field -> conflict (force required)
func TestThreeWay_C_UnsetVsExplicitDelete(t *testing.T) {
	sc := widgetSchema(t)

	steps := []goldenStep{
		{
			name:     "sre builds replicas+image",
			manager:  "sre",
			config:   `{"replicas": 4, "image": "registry/widget:9"}`,
			wantLive: `{"replicas": 4, "image": "registry/widget:9"}`,
			wantOwners: map[string][]string{
				"replicas": {"sre"},
				"image":    {"sre"},
			},
			rationale: "baseline owned by sre",
		},
		{
			name:     "sre omits image entirely -> retract own field",
			manager:  "sre",
			config:   `{"replicas": 4}`,
			wantLive: `{"replicas": 4}`,
			wantOwners: map[string][]string{
				"replicas": {"sre"},
			},
			wantChangesOps: map[string][]string{
				"image": {"delete"},
			},
			rationale: "absence retracts what you own: image disappears",
		},
		{
			name:     "net creates image while sre keeps replicas (disjoint again)",
			manager:  "net",
			config:   `{"image": "registry/widget:10"}`,
			wantLive: `{"replicas": 4, "image": "registry/widget:10"}`,
			wantOwners: map[string][]string{
				"replicas": {"sre"},
				"image":    {"net"},
			},
			rationale: "image now belongs to net",
		},
		{
			name:     "sre omits image again -> foreign field must be untouched",
			manager:  "sre",
			config:   `{"replicas": 4}`,
			wantLive: `{"replicas": 4, "image": "registry/widget:10"}`,
			wantOwners: map[string][]string{
				"replicas": {"sre"},
				"image":    {"net"},
			},
			rationale: "unsubmitted foreign field is NOT deleted and raises NO conflict",
		},
		{
			name:    "sre explicitly nulls image without force -> conflict",
			manager: "sre",
			config:  `{"replicas": 4, "image": null}`,
			expectConflicts: map[string]string{
				"image": merge.ReasonExplicitDelete,
			},
			rationale: "explicit delete on another manager's field is a conflict",
		},
		{
			name:     "sre explicitly nulls image WITH force",
			manager:  "sre",
			force:    true,
			config:   `{"replicas": 4, "image": null}`,
			wantLive: `{"replicas": 4}`,
			wantOwners: map[string][]string{
				"replicas": {"sre"},
			},
			wantChangesOps: map[string][]string{
				"image": {"takeover"},
			},
			rationale: "force delete succeeds; replicas (sre) unaffected",
		},
		{
			name:       "explicit null on the manager's OWN field is a clean delete",
			manager:    "sre",
			config:     `{"replicas": null}`,
			wantLive:   `{}`,
			wantOwners: map[string][]string{},
			wantChangesOps: map[string][]string{
				"replicas": {"delete"},
			},
			rationale: "self-owned explicit null deletes without force or conflict",
		},
	}
	runGolden(t, sc, steps)
}
