package tests

import (
	"context"
	"path"
	"testing"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
	"clsnap/internal/replay"
	"clsnap/tests/harness"
)

// TestRestartAbortsUnfinishedRound drives the restart fixture until S1 is
// open on n1 and n2 (n3 never participates), restarts n1 and n2, and asserts:
//
//   - the durable S1 records are marked aborted,
//   - independent replay refuses to assemble S1 (snapshot_incomplete),
//   - a stale epoch-1 marker is rejected with state_conflict/round_stale,
//   - re-initiating the SAME id is refused (aborted); a NEW round id works,
//   - no balances from two eras are stitched: fresh S2 still conserves 100.
func TestRestartAbortsUnfinishedRound(t *testing.T) {
	sc, err := harness.LoadScenario(path.Join("..", "testdata", "scenarios", "restart.json"))
	if err != nil {
		t.Fatal(err)
	}
	h, err := harness.New(context.Background(), sc, t.TempDir(), "test-restart")
	if err != nil {
		t.Fatal(err)
	}
	defer h.Close()
	ctx := context.Background()
	if err := h.Cluster().Run(ctx, sc); err != nil {
		t.Fatalf("scenario lead-in: %v", err)
	}

	// Pre-restart: n1 and n2 must be recording, n3 must have no record.
	for _, n := range []protocol.NodeID{"n1", "n2"} {
		rec, err := h.Store(n).GetRecord(ctx, n, "S1")
		if err != nil {
			t.Fatalf("record %s: %v", n, err)
		}
		if rec.Phase != protocol.PhaseRecording {
			t.Fatalf("%s phase pre-restart = %s, want recording", n, rec.Phase)
		}
	}
	if _, err := h.Store("n3").GetRecord(ctx, "n3", "S1"); !apperr.IsKind(err, apperr.KindInput) {
		t.Fatalf("n3 should have no S1 record, got %v", err)
	}

	// Restart all three processes (a whole-round restart).
	for _, n := range h.NodeIDs() {
		if _, epoch, err := h.RestartNode(ctx, n); err != nil {
			t.Fatalf("restart %s: %v epoch=%d", n, err, epoch)
		}
	}
	// Tear down the network-side remnants of S1: receivers would reject them
	// (asserted below), but dropping them models the aborted round being fully
	// withdrawn so a fresh round cannot be blocked behind them.
	h.Cluster().Dir.DropMarkersFor("S1")

	for _, n := range []protocol.NodeID{"n1", "n2"} {
		rec, err := h.Store(n).GetRecord(ctx, n, "S1")
		if err != nil {
			t.Fatalf("record %s after restart: %v", n, err)
		}
		if rec.Phase != protocol.PhaseAborted {
			t.Fatalf("%s phase after restart = %s, want aborted", n, rec.Phase)
		}
		if rec.Reason == "" {
			t.Fatalf("%s abort reason empty", n)
		}
	}
	// n3 never joined S1: it has no record (nothing to abort or stitch).
	if _, err := h.Store("n3").GetRecord(ctx, "n3", "S1"); !apperr.IsKind(err, apperr.KindInput) {
		t.Fatalf("n3 unexpectedly has S1 record: %v", err)
	}

	// Independent replay must refuse to assemble the aborted/partial round.
	view := storesView(h)
	if gv, err := replay.Assemble(ctx, view, h.NodeIDs(), "S1"); err == nil {
		t.Fatalf("replay assembled an aborted round: %+v", gv)
	} else {
		ae, ok := apperr.As(err)
		if !ok || ae.Kind != apperr.KindConflict || ae.Code != apperr.CodeSnapshotIncomplete {
			t.Fatalf("replay abort err = %v, want state_conflict/snapshot_incomplete", err)
		}
	}

	// A stale epoch-1 marker delivered to restarted n2 must be rejected.
	stale := protocol.Envelope{
		Type: protocol.MsgMarker, Src: "n1", Dst: "n2", Lamport: 1,
		Marker: &protocol.Marker{Snapshot: "S1", Epoch: 1},
	}
	err = h.Coord("n2").Receive(ctx, stale)
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindConflict || ae.Code != apperr.CodeRoundStale {
		t.Fatalf("stale marker err = %v, want state_conflict/round_stale_after_restart", err)
	}

	// Re-initiating the SAME aborted id is refused so nobody stitches eras.
	err = h.Coord("n1").Initiate(ctx, "S1")
	ae, ok = apperr.As(err)
	if !ok || ae.Kind != apperr.KindConflict || ae.Code != apperr.CodeSnapshotAborted {
		t.Fatalf("re-initiate aborted id: %v", err)
	}

	// A FRESH round id completes and still conserves exactly 100, proving the
	// new incarnation is internally consistent (no mixed-era state).
	if err := h.Coord("n1").Initiate(ctx, "S2"); err != nil {
		t.Fatal(err)
	}
	drainAll(ctx, h)
	gv, err := replay.Assemble(ctx, view, h.NodeIDs(), "S2")
	if err != nil {
		t.Fatalf("S2 assemble: %v", err)
	}
	if err := replay.Conservation(gv, 100); err != nil {
		t.Fatalf("post-restart S2: %v", err)
	}
}
