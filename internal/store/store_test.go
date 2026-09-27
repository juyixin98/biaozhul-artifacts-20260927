package store_test

import (
	"context"
	"encoding/json"
	"testing"

	"igmpq/internal/cats"
	"igmpq/internal/model"
	"igmpq/internal/replay"
	"igmpq/internal/store"
)

func scenario() replay.Scenario {
	runUntil := int64(100000)
	return replay.Scenario{
		Name:   "store-roundtrip",
		Config: json.RawMessage(`{"interfaces":["eth0"],"query_interval_sec":100,"query_response_interval_sec":10,"robustness_variable":2,"last_member_query_interval_sec":5,"last_member_query_count":3}`),
		Events: []model.Event{
			{TimeMS: 0, Type: model.EventReport, Iface: "eth0", Group: "239.1.1.1", Member: "10.0.0.1"},
			{TimeMS: 50000, Type: model.EventLeave, Iface: "eth0", Group: "239.1.1.1", Member: "10.0.0.1"},
		},
		RunUntil: &runUntil,
	}
}

func TestSaveAndLoadRoundTrip(t *testing.T) {
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer st.Close()

	sc := scenario()
	res, err := replay.Run(sc)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	ctx := context.Background()
	id, err := st.SaveRun(ctx, sc, res)
	if err != nil {
		t.Fatalf("save: %v", err)
	}
	if id != 1 {
		t.Fatalf("run id=%d, want 1", id)
	}

	loaded, err := st.LoadResult(ctx, id)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	wantJSON, _ := json.Marshal(res)
	gotJSON, _ := json.Marshal(loaded)
	// loaded carries RunID; clear before comparing
	res.RunID = 0
	loaded.RunID = 0
	wantJSON, _ = json.Marshal(res)
	gotJSON, _ = json.Marshal(loaded)
	if string(wantJSON) != string(gotJSON) {
		t.Fatalf("round trip mismatch\ngot:  %s\nwant: %s", gotJSON, wantJSON)
	}

	// Spot-check the stored content: last-member deadline 50000+15000.
	var foundLMQ bool
	for _, tr := range loaded.Transitions {
		if tr.Type == "last_member_query_started" {
			foundLMQ = true
		}
	}
	if !foundLMQ {
		t.Fatal("stored result lost the last_member_query_started transition")
	}
	ivs := loaded.Intervals["eth0/239.1.1.1"]
	if len(ivs) != 1 || ivs[0].EndMS == nil || *ivs[0].EndMS != 65000 {
		t.Fatalf("stored intervals=%+v, want [0,65000)", ivs)
	}

	runs, err := st.ListRuns(ctx)
	if err != nil || len(runs) != 1 || runs[0].Name != "store-roundtrip" {
		t.Fatalf("ListRuns=%+v, err=%v", runs, err)
	}
}

func TestLoadMissingRun(t *testing.T) {
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer st.Close()
	_, err = st.LoadResult(context.Background(), 42)
	if cats.CategoryOf(err) != cats.RunNotFound {
		t.Fatalf("category=%s, want run_not_found", cats.CategoryOf(err))
	}
}
