package store

import (
	"context"
	"testing"

	"tcpreasm/internal/diag"
)

func ctx() context.Context { return context.Background() }

func diagRecord(req, cat string, seq, end uint64) diag.Record {
	return diag.Record{
		RequestID: req, Decision: diag.Accepted, Category: diag.Category(cat),
		SegSeqAbs: seq, SegEndAbs: end,
	}
}

func TestGapReconcileAndFill(t *testing.T) {
	st, err := Open(ctx(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	mkConn := func() {
		if err := st.UpsertConnection(ctx(), ConnectionRow{
			FlowKey: "f", EndpointA: "a", EndpointB: "b", ClientEP: "a",
			State: "ESTABLISHED", CreatedSeq: 1, UpdatedSeq: 1,
		}); err != nil {
			t.Fatal(err)
		}
	}
	mkConn()
	fk, g, dir := "f", 1, "c2s"

	if err := st.ReconcileOpenGap(ctx(), fk, g, dir, 0, 30, 1); err != nil {
		t.Fatal(err)
	}
	gaps, err := st.OpenGaps(ctx(), fk, g, dir)
	if err != nil || len(gaps) != 1 || gaps[0] != [2]uint64{0, 30} {
		t.Fatalf("reconcile new gap wrong: %v %v", gaps, err)
	}
	// Reconcile a shrinking overlapping gap replaces the row.
	if err := st.ReconcileOpenGap(ctx(), fk, g, dir, 15, 30, 2); err != nil {
		t.Fatal(err)
	}
	gaps, _ = st.OpenGaps(ctx(), fk, g, dir)
	if len(gaps) != 1 || gaps[0] != [2]uint64{15, 30} {
		t.Fatalf("reconcile shrink wrong: %v", gaps)
	}
	// Filling up to frontier 30 closes it.
	if err := st.FillGapsUpTo(ctx(), fk, g, dir, 30, "pkt-fill", 3); err != nil {
		t.Fatal(err)
	}
	gaps, _ = st.OpenGaps(ctx(), fk, g, dir)
	if len(gaps) != 0 {
		t.Fatalf("gap must be filled, got %v", gaps)
	}
	rows, err := st.ListGaps(ctx(), fk, g, dir, "filled")
	if err != nil || len(rows) != 1 || rows[0].FilledRecordID != "pkt-fill" {
		t.Fatalf("filled evidence wrong: %v %v", rows, err)
	}
}

func TestGapStraddleFrontierShrinks(t *testing.T) {
	st, _ := Open(ctx(), ":memory:")
	defer st.Close()
	_ = st.UpsertConnection(ctx(), ConnectionRow{
		FlowKey: "f", EndpointA: "a", EndpointB: "b", ClientEP: "a",
		State: "ESTABLISHED", CreatedSeq: 1, UpdatedSeq: 1,
	})
	if err := st.ReconcileOpenGap(ctx(), "f", 1, "c2s", 10, 40, 1); err != nil {
		t.Fatal(err)
	}
	if err := st.FillGapsUpTo(ctx(), "f", 1, "c2s", 25, "p", 2); err != nil {
		t.Fatal(err)
	}
	gaps, _ := st.OpenGaps(ctx(), "f", 1, "c2s")
	if len(gaps) != 1 || gaps[0] != [2]uint64{25, 40} {
		t.Fatalf("straddling gap must shrink to [25,40): %v", gaps)
	}
}

func TestPacketIdempotentInsert(t *testing.T) {
	st, _ := Open(ctx(), ":memory:")
	defer st.Close()
	row := PacketRow{RecordID: "r1", Source: "capture", ObsOrder: 1, Seq32: 5}
	if err := st.InsertPacket(ctx(), row); err != nil {
		t.Fatal(err)
	}
	if err := st.InsertPacket(ctx(), row); err != nil {
		t.Fatalf("duplicate insert must be ignored: %v", err)
	}
	exists, err := st.PacketExists(ctx(), "capture", "r1")
	if err != nil || !exists {
		t.Fatalf("idempotent lookup wrong: %v %v", exists, err)
	}
}

func TestDiagnosticsRoundTrip(t *testing.T) {
	st, _ := Open(ctx(), ":memory:")
	defer st.Close()
	if err := st.InsertDiagnosticRow(ctx(), diagRecord("req-9", "cat-x", 123456789, 987654321), 7); err != nil {
		t.Fatal(err)
	}
	recs, err := st.ListDiagnostics(ctx(), "req-9", "", -1, "cat-x", 10)
	if err != nil || len(recs) != 1 {
		t.Fatalf("diag roundtrip: %v %v", recs, err)
	}
	if recs[0].SegSeqAbs != 123456789 || recs[0].SegEndAbs != 987654321 {
		t.Fatal("absolute seq values must round-trip")
	}
}
