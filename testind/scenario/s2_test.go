package scenario_test

import (
	"testing"

	"igmpv2timer/internal/engine"
	"igmpv2timer/internal/model"
)

// TestS2QueryLossAndStaleRound answers:
//   - when the second general query is lost in transit, does the entry
//     survive exactly one Group Membership Interval after the last report
//     and no longer? (549 present, 550 absent — exact boundary)
//   - is a late report answering the superseded first round classified
//     STALE rather than recreating the deleted group?
//   - is the retention interval exactly [100, 550]?
func TestS2QueryLossAndStaleRound(t *testing.T) {
	rep, exp, cfg, st := runBoth(t, "s2_query_loss_stale.json")
	assertAllEngineAssertionsPass(t, rep)
	crossCheckVerdicts(t, rep, exp)

	// The lost query still bumps the generation (router emitted it) but the
	// DROPPED record names the transit failure and gen 2.
	drop := findCoreDiag(t, rep, model.VDropped, "",
		model.ReasonQueryDropped, 450)
	if drop.GenActive != 2 {
		t.Errorf("dropped query gen = %d, want 2", drop.GenActive)
	}

	// Timeout at the exact GMI boundary after the last accepted report (250).
	to := findCoreDiag(t, rep, model.VTimeout, "",
		model.ReasonMembershipTimeout, 550)
	if to.At != 550 {
		t.Errorf("timeout at %d, want exactly 550", to.At)
	}

	// Late report at 700 answers gen 1 while gen 2 is current and the group
	// is gone -> STALE, must NOT recreate state.
	stale := findCoreDiag(t, rep, model.VStale, "hostA",
		model.ReasonStaleRound, 700)
	if stale.GenActive != 2 || stale.GenApplied != 1 {
		t.Errorf("stale gens active=%d applied=%d, want 2/1",
			stale.GenActive, stale.GenApplied)
	}
	if stale.MembershipDeadline != 0 {
		t.Errorf("stale report must not set a deadline, got %d",
			stale.MembershipDeadline)
	}
	if len(stale.Members) != 0 {
		t.Errorf("stale report must not register members, got %v", stale.Members)
	}

	// Retention interval exactly bounded by creation and timeout.
	if len(rep.Intervals) != 1 {
		t.Fatalf("intervals = %v", rep.Intervals)
	}
	iv := rep.Intervals[0]
	if int64(iv.Start) != 100 || int64(iv.End) != 550 {
		t.Errorf("retention = %d..%d, want 100..550", iv.Start, iv.End)
	}
	if iv.Reason != model.ReasonMembershipTimeout {
		t.Errorf("retention end reason = %q", iv.Reason)
	}

	// Final state empty; oracle independently agrees.
	if len(rep.FinalSnapshot.Groups) != 0 {
		t.Errorf("final groups = %v, want empty", rep.FinalSnapshot.Groups)
	}
	if exp.FinalPresent {
		t.Error("oracle unexpectedly predicts present group")
	}

	// Rebuild from the SQLite event journal reproduces the identical closed
	// interval — persistence is not just "writable".
	path := scenarioPath(t, "s2_query_loss_stale.json")
	script, err := engine.LoadScript(path)
	if err != nil {
		t.Fatal(err)
	}
	rb, err := engine.RebuildFromJournal(cfg, st, script.Until(), rep)
	if err != nil {
		t.Fatal(err)
	}
	if !rb.OK {
		t.Errorf("journal rebuild diverged: %s", rb.Detail)
	}
}
