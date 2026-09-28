package merge_test

import (
	"encoding/json"
	"sort"
	"testing"

	"fieldmerge/internal/apperr"
	"fieldmerge/internal/fieldpath"
	"fieldmerge/internal/merge"
	"fieldmerge/internal/schema"
	"fieldmerge/internal/testkit"
)

// goldenStep is one scripted apply against a running three-way state.
// Expected values below are HAND-AUTHORED fixtures (see the doc comments in
// each test): they are the answer to a manual three-way merge, never output
// captured from the engine. A mismatch means the engine disagrees with the
// independently derived result.
type goldenStep struct {
	name       string
	manager    string
	force      bool
	config     string
	wantLive   string
	wantOwners map[string][]string
	// expectConflicts: path -> reason; empty step means "must commit cleanly"
	expectConflicts map[string]string
	wantChangesOps  map[string][]string // path -> ops, for mutation audit
	rationale       string
}

type goldenState struct {
	live   json.RawMessage
	claims []merge.Claim
}

func widgetSchema(t *testing.T) *schema.Schema {
	t.Helper()
	sc, err := schema.New("widget", map[string]schema.ListDecl{
		"ingresses": {Type: schema.ListMap, KeyName: "name"},
		"tags":      {Type: schema.ListSet},
		"servers":   {Type: schema.ListAtomic},
	})
	if err != nil {
		t.Fatalf("schema: %v", err)
	}
	return sc
}

func runGolden(t *testing.T, sc *schema.Schema, steps []goldenStep) {
	t.Helper()
	rec := testkit.NewRecorder(t, t.Name())
	defer rec.Close()

	st := goldenState{live: json.RawMessage(`{}`), claims: nil}
	for i, step := range steps {
		rec.Step("apply_input", map[string]any{
			"step": i + 1, "name": step.name, "manager": step.manager,
			"force": step.force, "config": rawJSON(step.config),
		}, step.rationale)

		res, err := merge.Apply(merge.Input{
			Kind: "widget", Name: "w1", Manager: step.manager, Force: step.force,
			Live: st.live, Config: json.RawMessage(step.config), Schema: sc,
		}, st.claims)
		if err != nil {
			t.Fatalf("step %d (%s): unexpected engine error: %v", i+1, step.name, err)
		}

		rec.Step("apply_output", map[string]any{
			"step": i + 1, "live": res.Live,
			"conflicts": res.Conflict, "changes": res.Changes,
			"pruned": res.PrunedOwnership,
		}, "actual intermediate state produced by engine")

		// Conflict assertions: exact paths and reasons (not just "it failed").
		gotConflicts := map[string]string{}
		for _, c := range res.Conflict {
			gotConflicts[c.Path] = c.Reason
		}
		if !eqStringMap(gotConflicts, step.expectConflicts) {
			rec.Check("conflicts", false, "conflict paths/reasons differ",
				gotConflicts, step.expectConflicts)
			t.Fatalf("step %d (%s): conflicts = %v, want %v",
				i+1, step.name, gotConflicts, step.expectConflicts)
		}
		rec.Check("conflicts", true, "conflict set matches hand-derived expectation",
			gotConflicts, step.expectConflicts)

		if len(step.expectConflicts) > 0 {
			// On conflict nothing is committed: state must be unchanged.
			continue
		}

		var want any
		if err := json.Unmarshal([]byte(step.wantLive), &want); err != nil {
			t.Fatalf("bad fixture live: %v", err)
		}
		if !jsonEqual(res.Live, want) {
			rec.Check("live", false, "merged live differs from hand-computed three-way result",
				res.Live, want)
			t.Fatalf("step %d (%s): live mismatch\n got: %s\nwant: %s",
				i+1, step.name, mustJSON(res.Live), step.wantLive)
		}
		rec.Check("live", true, "merged live equals hand-computed three-way result",
			res.Live, want)

		gotOwners := map[string][]string{}
		for _, c := range res.Claims {
			gotOwners[c.Path] = c.Managers
		}
		if !eqOwners(gotOwners, step.wantOwners) {
			rec.Check("ownership", false, "field ownership differs",
				gotOwners, step.wantOwners)
			t.Fatalf("step %d (%s): ownership mismatch\n got: %s\nwant: %s",
				i+1, step.name, mustJSON(gotOwners), mustJSON(step.wantOwners))
		}
		rec.Check("ownership", true, "ownership matches hand-derived field claims",
			gotOwners, step.wantOwners)

		if step.wantChangesOps != nil {
			got := map[string][]string{}
			for _, ch := range res.Changes {
				got[ch.Path] = append(got[ch.Path], ch.Op)
			}
			if !eqOps(got, step.wantChangesOps) {
				rec.Check("changes", false, "change ops differ", got, step.wantChangesOps)
				t.Fatalf("step %d (%s): changes mismatch\n got: %s\nwant: %s",
					i+1, step.name, mustJSON(got), mustJSON(step.wantChangesOps))
			}
			rec.Check("changes", true, "audited change operations match", got, step.wantChangesOps)
		}

		// Commit: advance the three-way state exactly like the store would.
		st.live = json.RawMessage(mustJSON(res.Live))
		st.claims = res.Claims
	}
}

func rawJSON(s string) any {
	var v any
	_ = json.Unmarshal([]byte(s), &v)
	return v
}

func jsonEqual(a, b any) bool {
	ab, _ := json.Marshal(a)
	bb, _ := json.Marshal(b)
	var av, bv any
	_ = json.Unmarshal(ab, &av)
	_ = json.Unmarshal(bb, &bv)
	return mustJSON(av) == mustJSON(bv)
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "<err>"
	}
	return string(b)
}

func eqStringMap(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

func eqOwners(got map[string][]string, want map[string][]string) bool {
	if len(got) != len(want) {
		return false
	}
	for p, wantMgrs := range want {
		gotMgrs, ok := got[p]
		if !ok {
			return false
		}
		if !eqSortedStrings(gotMgrs, wantMgrs) {
			return false
		}
	}
	return true
}

func eqOps(got, want map[string][]string) bool {
	if len(got) != len(want) {
		return false
	}
	for p, wo := range want {
		if !eqSortedStrings(got[p], wo) {
			return false
		}
	}
	return true
}

func eqSortedStrings(a, b []string) bool {
	x := append([]string{}, a...)
	y := append([]string{}, b...)
	sort.Strings(x)
	sort.Strings(y)
	if len(x) != len(y) {
		return false
	}
	for i := range x {
		if x[i] != y[i] {
			return false
		}
	}
	return true
}

// keep the imports referenced even if a test is trimmed.
var (
	_ = apperr.InvalidInput
	_ = fieldpath.Path{}
)
