package scenario_test

import (
	"testing"

	"igmpv2timer/internal/engine"
	"igmpv2timer/internal/model"
)

// TestS1InterleavedReports answers: with two members whose per-round random
// delays alternate, which member reports and which is suppressed each
// round? Are membership deadlines refreshed to exact GMI boundaries? Is
// the forwarding entry retained continuously?
func TestS1InterleavedReports(t *testing.T) {
	rep, exp, cfg, _ := runBoth(t, "s1_interleaved_reports.json")
	assertAllEngineAssertionsPass(t, rep)
	crossCheckVerdicts(t, rep, exp)

	gmi := cfg.Timing.GroupMembershipInterval // 260

	// Round 1 (query at 250): hostB delay 20 -> reports first at 270,
	// hostA delay 40 -> its report at 290 is suppressed.
	d := findCoreDiag(t, rep, model.VAccepted, "hostB",
		model.ReasonMembershipRefreshed, 270)
	if int64(d.MembershipDeadline) != 270+gmi {
		t.Errorf("r1 deadline = %d, want %d", d.MembershipDeadline, 270+gmi)
	}
	if d.GenActive != 1 || d.GenApplied != 1 {
		t.Errorf("r1 generations active=%d applied=%d, want 1/1", d.GenActive, d.GenApplied)
	}
	if got := d.Members; len(got) != 2 || got[0] != "hostA" || got[1] != "hostB" {
		t.Errorf("r1 members = %v, want [hostA hostB]", got)
	}
	s := findCoreDiag(t, rep, model.VSuppressed, "hostA",
		model.ReasonReportSuppressed, 290)
	if s.GenActive != 1 || s.GenApplied != 1 {
		t.Errorf("suppressed r1 gens = %d/%d, want 1/1", s.GenActive, s.GenApplied)
	}

	// Round 2 (query at 450): hostA delay 20 -> 470 accepted; hostB 490
	// suppressed. The roles alternate, proving per-round delay redraw.
	d = findCoreDiag(t, rep, model.VAccepted, "hostA",
		model.ReasonMembershipRefreshed, 470)
	if int64(d.MembershipDeadline) != 470+gmi {
		t.Errorf("r2 deadline = %d, want %d", d.MembershipDeadline, 470+gmi)
	}
	if d.GenActive != 2 || d.GenApplied != 2 {
		t.Errorf("r2 gens = %d/%d, want 2/2", d.GenActive, d.GenApplied)
	}
	findCoreDiag(t, rep, model.VSuppressed, "hostB",
		model.ReasonReportSuppressed, 490)

	// Round 3: roles flip back (B reports 670, A suppressed 690).
	d = findCoreDiag(t, rep, model.VAccepted, "hostB",
		model.ReasonMembershipRefreshed, 670)
	if d.GenActive != 3 {
		t.Errorf("r3 active gen = %d, want 3", d.GenActive)
	}

	// Exactly four suppressions over four rounds.
	nSupp := 0
	for _, x := range rep.Diags {
		if x.Verdict == model.VSuppressed {
			nSupp++
		}
	}
	if nSupp != 4 {
		t.Errorf("suppression count = %d, want 4", nSupp)
	}

	// Forwarding entry: one continuous open interval from 100 (never closed
	// within the run) — retention is NOT reset by interleaved reports.
	if len(rep.Intervals) != 1 {
		t.Fatalf("intervals = %v, want exactly 1", rep.Intervals)
	}
	iv := rep.Intervals[0]
	if int64(iv.Start) != 100 || int64(iv.End) != 0 {
		t.Errorf("retention interval = %d..%d, want 100..open", iv.Start, iv.End)
	}

	// Oracle must independently predict the same final membership.
	if !exp.FinalPresent {
		t.Error("oracle: group should be present at run end")
	}
	if len(exp.FinalMembers) != 2 {
		t.Errorf("oracle final members = %v, want both", exp.FinalMembers)
	}
}

// TestS1FailureCategoryShape guards that the assertion result vocabulary
// used by every test is the documented category constants.
func TestS1FailureCategoryShape(t *testing.T) {
	cases := map[string]string{
		"present":  engine.FailExpectedPresent,
		"absent":   engine.FailExpectedAbsent,
		"diag":     engine.FailDiagNotFound,
		"count":    engine.FailCountMismatch,
		"interval": engine.FailIntervalMismatch,
		"gen":      engine.FailGenMismatch,
	}
	for name, cat := range cases {
		if cat == "" || name == "" {
			t.Errorf("empty failure category for %s", name)
		}
	}
}
