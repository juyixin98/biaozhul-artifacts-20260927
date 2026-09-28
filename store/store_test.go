package store_test

import (
	"context"
	"errors"
	"testing"

	"pvsim/config"
	"pvsim/engine"
	"pvsim/store"
)

func TestStoreRoundTrip(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer st.Close()

	sc, err := config.LoadFile("../fixtures/01_multi_exit.json")
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	col := engine.NewCollector()
	res, err := engine.Run(sc, engine.Options{}, col)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !res.Converged {
		t.Fatalf("fixture must converge")
	}
	rec := store.RunRecord{
		RunID: "run-test-1", Name: sc.Name, Status: store.StatusOK,
		Converged: true, Steps: res.Steps, Versions: res.Versions,
		ScenarioJSON: `{"name":"A"}`, ResultJSON: `{"converged":true}`,
	}
	if err := st.SaveRun(ctx, store.SaveParams{Record: rec, Collector: col}); err != nil {
		t.Fatalf("save: %v", err)
	}
	// Duplicate id -> ErrExists (surfaced upstream as STATE_CONFLICT).
	if err := st.SaveRun(ctx, store.SaveParams{Record: rec}); !errors.Is(err, store.ErrExists) {
		t.Fatalf("second save err = %v, want ErrExists", err)
	}

	got, err := st.GetRun(ctx, "run-test-1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.Name != sc.Name || !got.Converged || got.Status != store.StatusOK {
		t.Fatalf("loaded record mismatch: %+v", got)
	}
	if got.Steps != res.Steps {
		t.Fatalf("steps persisted = %d, want %d", got.Steps, res.Steps)
	}

	// Artifacts.
	del, err := st.GetDeliveries(ctx, "run-test-1")
	if err != nil || len(del) != 2 {
		t.Fatalf("deliveries = %d,%v; want 2", len(del), err)
	}
	if del[0].Seq != 1 || del[0].Version >= del[1].Version {
		t.Fatalf("delivery ordering wrong: %+v", del)
	}
	decs, err := st.GetDecisions(ctx, "run-test-1")
	if err != nil {
		t.Fatalf("decisions: %v", err)
	}
	if len(decs) == 0 {
		t.Fatalf("no decisions persisted")
	}
	// r3 must pick r1 for the prefix.
	var r3 string
	for _, d := range decs {
		if d.Router == "r3" && d.Prefix == "203.0.113.0/24" {
			r3 = d.ChosenPeer
		}
	}
	if r3 != "r1" {
		t.Fatalf("r3 final chosen = %q, want r1", r3)
	}
	tr, err := st.GetTraces(ctx, "run-test-1")
	if err != nil || len(tr) == 0 {
		t.Fatalf("traces = %d,%v", len(tr), err)
	}
}

func TestStoreNotFound(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer st.Close()
	if _, err := st.GetRun(ctx, "missing"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("GetRun missing err = %v, want ErrNotFound", err)
	}
	if _, err := st.GetTraces(ctx, "missing"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("GetTraces missing err = %v, want ErrNotFound", err)
	}
}

func TestStoreListRuns(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer st.Close()
	for _, id := range []string{"a", "b", "c"} {
		if err := st.SaveRun(ctx, store.SaveParams{
			Record: store.RunRecord{RunID: id, Name: id, Status: store.StatusOK, Converged: true},
		}); err != nil {
			t.Fatalf("save %s: %v", id, err)
		}
	}
	runs, err := st.ListRuns(ctx, 10)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(runs) != 3 {
		t.Fatalf("listed %d runs, want 3", len(runs))
	}
}
