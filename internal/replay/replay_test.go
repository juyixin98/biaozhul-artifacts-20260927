package replay_test

import (
	"context"
	"encoding/json"
	"path/filepath"
	"strconv"
	"testing"

	"flowrouter/internal/apperr"
	"flowrouter/internal/config"
	"flowrouter/internal/replay"
	"flowrouter/internal/router"
	"flowrouter/internal/store"
	"flowrouter/internal/testutil"
)

type harness struct {
	ctx     context.Context
	st      *store.Store
	rt      *router.Router
	planner *replay.Planner
	lib     *replay.FileLibrary
}

func newHarness(t *testing.T, vpw, capv int) *harness {
	t.Helper()
	h := &harness{ctx: context.Background()}
	var err error
	h.st, err = store.Open(h.ctx, ":memory:", 500)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = h.st.Close() })
	h.rt = router.New(vpw, capv)
	h.planner = replay.NewPlanner(h.st, vpw, capv)
	h.lib = replay.NewFileLibrary(filepath.Join(testutil.RepoRoot(t), "testdata", "flowsets"))
	return h
}

// publishVersion builds the given scenario version through the real router and
// persists it exactly like the API layer would.
func (h *harness) publishVersion(t *testing.T, ver testutil.ScenarioVer, note string) {
	t.Helper()
	var ms []config.Member
	down := map[string]bool{}
	reasons := map[string]string{}
	for _, m := range ver.Members {
		ms = append(ms, config.Member{ID: m.ID, Weight: m.Weight})
		if !m.IsUp() {
			down[m.ID] = true
			reasons[m.ID] = "test"
		}
	}
	var snap *router.Snapshot
	var changed bool
	var err error
	if h.rt.Current().Version == 0 {
		snap, changed, err = h.rt.Load(ms, down)
	} else {
		snap, changed, err = h.rt.ReplaceMembers(h.rt.Current().Version, ms, down, reasons)
	}
	if err != nil {
		t.Fatalf("%s: %v", note, err)
	}
	if !changed && note != "initial-empty" {
		// identical input is possible only in s4 v2; handle by force-different
		t.Fatalf("%s produced no new version (fingerprint identical)", note)
	}
	membersJSON, allocJSON := snapshotJSON(t, snap)
	if err := h.st.InsertRingVersion(h.ctx, store.RingVersionRow{
		Version: snap.Version, MembersJSON: membersJSON,
		AllocationJSON: allocJSON, Fingerprint: snap.Fingerprint,
	}); err != nil {
		t.Fatal(err)
	}
}

func snapshotJSON(t *testing.T, snap *router.Snapshot) ([]byte, []byte) {
	t.Helper()
	mj, _ := json.Marshal(snap.Members)
	a := snap.Ring.Allocation()
	aj, _ := json.Marshal(map[string]any{
		"counts": a.Counts, "base": a.Base, "extra": a.Extra, "total": a.Total,
		"strategy": a.Strategy, "ideal": a.Ideal, "remainder": a.Remainder,
		"vnodes_per_weight": a.VNodesPerW, "capped_to": a.CappedTo,
	})
	return mj, aj
}

func TestReplayMatchesGolden(t *testing.T) {
	matrix := testutil.LoadScenarios(t)
	for _, sc := range matrix.Scenarios {
		vpw := matrix.VNodesPerWeight
		capv := matrix.MaxVNodes
		if sc.MaxVNodes != 0 {
			capv = sc.MaxVNodes
		}
		golden := testutil.LoadGolden(t, "flows_smoke")
		gs := golden.Scenarios[sc.Name]

		t.Run(sc.Name, func(t *testing.T) {
			h := newHarness(t, vpw, capv)
			for i, ver := range sc.Versions {
				h.publishVersion(t, ver, sc.Name+"-v"+strconv.Itoa(i))
			}
			set, err := h.lib.Load("flows_smoke")
			if err != nil {
				t.Fatal(err)
			}
			trans := sc.Trans
			if len(trans) == 0 {
				for i := 0; i+1 < len(sc.Versions); i++ {
					trans = append(trans, [2]int{i, i + 1})
				}
			}
			// scenario version index i was persisted as ring version i+1
			for _, tr := range trans {
				res, err := h.planner.Execute(h.ctx, set, int64(tr[0]+1), int64(tr[1]+1))
				if err != nil {
					t.Fatalf("execute %v: %v", tr, err)
				}
				if res.Status != "COMPLETED" {
					t.Fatalf("status=%s error=%v", res.Status, res.Error)
				}
				want := gs.Transitions[strconv.Itoa(tr[0])+"->"+strconv.Itoa(tr[1])]
				if res.Diff.Moved != want.Moved || res.Diff.Stayed != want.Stayed {
					t.Errorf("moved/stayed got %d/%d want %d/%d",
						res.Diff.Moved, res.Diff.Stayed, want.Moved, want.Stayed)
				}
				if res.Diff.BaselineMoved != want.BaselineMoved {
					t.Errorf("baseline got %d want %d", res.Diff.BaselineMoved, want.BaselineMoved)
				}
				// the run must be fetchable with the same numbers
				fetched, err := h.planner.FetchRun(h.ctx, res.RunID)
				if err != nil {
					t.Fatal(err)
				}
				if fetched.Diff.Moved != want.Moved || len(fetched.Diff.Changes) != want.Moved {
					t.Errorf("fetched run moved=%d changes=%d want %d",
						fetched.Diff.Moved, len(fetched.Diff.Changes), want.Moved)
				}
			}
		})
	}
}

func TestReplayUnknownVersionClassifiedAndPersisted(t *testing.T) {
	h := newHarness(t, 160, 0)
	set, err := h.lib.Load("flows_smoke")
	if err != nil {
		t.Fatal(err)
	}
	res, err := h.planner.Execute(h.ctx, set, 1, 2)
	// no versions persisted: classified failure recorded, not an HTTP crash
	if err == nil {
		t.Fatal("expected error return for missing version")
	}
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindStateConflict || ae.Code != "UNKNOWN_VERSION" {
		t.Fatalf("err=%v", err)
	}
	if res == nil || res.Status != "FAILED" || res.Error == nil {
		t.Fatal("failed run result must be returned with classification")
	}
	if res.Error.Kind != string(apperr.KindStateConflict) {
		t.Errorf("error kind=%s", res.Error.Kind)
	}
	// the failure must be replayable: fetch the persisted FAILED row
	got, err := h.planner.FetchRun(h.ctx, res.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "FAILED" || got.Error.Code != "UNKNOWN_VERSION" {
		t.Fatalf("fetched=%+v", got)
	}
}

func TestFlowSetValidation(t *testing.T) {
	cases := []struct {
		name string
		doc  string
		code string
	}{
		{"not json", "{", "FLOWSET_MALFORMED"},
		{"no name", `{"flows":[{"src_ip":"10.0.0.1","dst_ip":"10.0.0.2","proto":6,"src_port":1,"dst_port":2}]}`, "FLOWSET_NO_NAME"},
		{"empty", `{"name":"x","flows":[]}`, "FLOWSET_EMPTY"},
		{"bad tuple", `{"name":"x","flows":[{"src_ip":"nope","dst_ip":"10.0.0.2","proto":6,"src_port":1,"dst_port":2}]}`, "FLOWSET_BAD_TUPLE"},
		{"dup tuple", `{"name":"x","flows":[
			{"src_ip":"10.0.0.1","dst_ip":"10.0.0.2","proto":6,"src_port":1,"dst_port":2},
			{"src_ip":"10.0.0.1","dst_ip":"10.0.0.2","proto":6,"src_port":1,"dst_port":2}]}`, "FLOWSET_DUP_TUPLE"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := replay.ParseFlowSet([]byte(tc.doc))
			ae, ok := apperr.As(err)
			if !ok || ae.Code != tc.code {
				t.Fatalf("err=%v want code %s", err, tc.code)
			}
		})
	}
}

func TestFileLibraryTraversalRejected(t *testing.T) {
	lib := replay.NewFileLibrary(filepath.Join(testutil.RepoRoot(t), "testdata", "flowsets"))
	if _, err := lib.Load("../scenarios/scenarios"); err == nil {
		t.Fatal("path traversal must be rejected")
	} else if ae, ok := apperr.As(err); !ok || ae.Code != "FLOWSET_BAD_NAME" {
		t.Fatalf("err=%v", err)
	}
	names, err := lib.Names()
	if err != nil {
		t.Fatal(err)
	}
	if len(names) < 2 {
		t.Fatalf("names=%v", names)
	}
}
