package scenario_test

import (
	"testing"

	"igmpv2timer/internal/model"
)

// TestS3LeaveAndRejoin answers:
//   - when hostA leaves but hostB remains, is the group retained with NO
//     group-specific queries (a single leave must not delete the group)?
//   - after the last member leaves, do exactly LMQC group-specific queries
//     run, and does a rejoin mid-sequence cancel deletion (generation guard)?
//   - does the second, unanswered leave delete at the exact boundary and
//     close a single continuous retention interval?
func TestS3LeaveAndRejoin(t *testing.T) {
	rep, exp, _, _ := runBoth(t, "s3_leave_rejoin.json")
	assertAllEngineAssertionsPass(t, rep)
	crossCheckVerdicts(t, rep, exp)

	// A leaves at 500, B remains: ACCEPTED/retained, members=[hostB], no LMQ.
	d := findCoreDiag(t, rep, model.VAccepted, "hostA",
		"member_left_group_retained", 500)
	if len(d.Members) != 1 || d.Members[0] != "hostB" {
		t.Errorf("after A leaves, members = %v, want [hostB]", d.Members)
	}

	// No GSQ may have been emitted before the last-member leave at 1000.
	for _, p := range rep.Emitted {
		if p.Packet == model.PktQueryGroup && int64(p.At) < 1000 {
			t.Errorf("group-specific query emitted at %d before last member left", p.At)
		}
	}

	// B leaves at 1000: last-member procedure starts. Per RFC 3376 the
	// FIRST group-specific query is sent immediately at 1000 and the
	// second at 1100; A rejoins at 1150, before the third query is due.
	findCoreDiag(t, rep, model.VAccepted, "hostB",
		"last_member_query_started", 1000)
	findCoreDiag(t, rep, model.VAccepted, "",
		"group_specific_query_emitted", 1000)
	findCoreDiag(t, rep, model.VAccepted, "",
		"group_specific_query_emitted", 1100)

	// A rejoins at 1150, between GSQ 2 and the cancelled GSQ 3:
	// report_during_lmq cancels deletion. The group keeps only hostA; the
	// third query of the FIRST sequence must NOT be emitted.
	rj := findCoreDiag(t, rep, model.VAccepted, "hostA",
		"report_during_lmq", 1150)
	if len(rj.Members) != 1 || rj.Members[0] != "hostA" {
		t.Errorf("after rejoin members = %v, want [hostA]", rj.Members)
	}
	nGSQ := 0
	for _, p := range rep.Emitted {
		if p.Packet == model.PktQueryGroup && int64(p.At) <= 1999 {
			nGSQ++
		}
	}
	if nGSQ != 2 {
		t.Errorf("first LMQ sequence emitted %d GSQs, want exactly 2 (3rd cancelled)", nGSQ)
	}

	// Second leave at 2000 starts a fresh sequence: immediate GSQ at 2000,
	// then 2100/2200; entry present at 2299, gone at the exact 2300 boundary
	// (leave + LMQC*LMQI = 2000 + 300).
	findCoreDiag(t, rep, model.VAccepted, "hostA",
		"last_member_query_started", 2000)
	findCoreDiag(t, rep, model.VTimeout, "",
		"last_member_query_confirmed", 2300)

	totalGSQ := 0
	for _, p := range rep.Emitted {
		if p.Packet == model.PktQueryGroup {
			totalGSQ++
		}
	}
	if totalGSQ != 5 {
		t.Errorf("total group-specific queries = %d, want 5 (2 cancelled seq + 3)",
			totalGSQ)
	}

	// One continuous retention interval for the entire 100..2300 lifetime —
	// the mid-LMQ rejoin must not split it.
	if len(rep.Intervals) != 1 {
		t.Fatalf("intervals = %v, want exactly 1", rep.Intervals)
	}
	iv := rep.Intervals[0]
	if int64(iv.Start) != 100 || int64(iv.End) != 2300 {
		t.Errorf("retention = %d..%d, want 100..2300", iv.Start, iv.End)
	}
	if iv.Reason != "last_member_query_confirmed" {
		t.Errorf("retention end reason = %q", iv.Reason)
	}

	if exp.FinalPresent {
		t.Error("oracle unexpectedly predicts a present group at end")
	}
	if len(exp.Intervals) != 1 ||
		exp.Intervals[0].Start != 100 || exp.Intervals[0].End != 2300 {
		t.Errorf("oracle intervals = %v, want one 100..2300", exp.Intervals)
	}
}
