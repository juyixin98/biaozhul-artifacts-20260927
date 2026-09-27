package engine_test

import (
	"testing"

	"igmpq/internal/cats"
	"igmpq/internal/config"
	"igmpq/internal/engine"
	"igmpq/internal/model"
	"igmpq/internal/simclock"
)

// testConfig is "config A" from testdata/scenarios/README.md:
// GMI = 2*100+10 = 210s, LMQT = 5*3 = 15s.
func testConfig(t *testing.T) config.Config {
	t.Helper()
	cfg, err := config.Normalize(config.Config{
		Interfaces:                 []string{"eth0"},
		QueryIntervalSec:           100,
		QueryResponseIntervalSec:   10,
		RobustnessVariable:         2,
		LastMemberQueryIntervalSec: 5,
		LastMemberQueryCount:       3,
	})
	if err != nil {
		t.Fatalf("normalize config: %v", err)
	}
	return cfg
}

func newEngine(t *testing.T) *engine.Engine {
	t.Helper()
	return engine.New(testConfig(t), simclock.NewManual(0))
}

func genPtr(g uint64) *uint64 { return &g }

func report(t int64, group, member string, gen *uint64) model.Event {
	return model.Event{TimeMS: t, Type: model.EventReport, Iface: "eth0", Group: group, Member: member, Gen: gen}
}

func leave(t int64, group, member string) model.Event {
	return model.Event{TimeMS: t, Type: model.EventLeave, Iface: "eth0", Group: group, Member: member}
}

func generalQuery(t int64) model.Event {
	return model.Event{TimeMS: t, Type: model.EventGeneralQuery, Iface: "eth0"}
}

// lastTransition returns the most recent transition of the given type, or nil.
func lastTransition(eng *engine.Engine, typ string) *engine.Transition {
	ts := eng.Transitions()
	for i := len(ts) - 1; i >= 0; i-- {
		if ts[i].Type == typ {
			return &ts[i]
		}
	}
	return nil
}

// TestTwoMembersInterleavedReportsAndLeaves is the core multi-member case:
// interleaved reports from two members, an explicit suppression, then the
// members leaving one by one. A single leave must NOT delete the group;
// the second leave starts the last-member phase with deadline
// 50000 + LMQT(15000) = 65000.
func TestTwoMembersInterleavedReportsAndLeaves(t *testing.T) {
	eng := newEngine(t)
	eng.Apply(report(0, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(report(10000, "239.1.1.1", "10.0.0.2", nil))
	eng.Apply(generalQuery(20000))
	eng.Apply(report(25000, "239.1.1.1", "10.0.0.1", genPtr(1)))

	// Suppression is audit-only: the group timer must stay at 25000+GMI.
	eng.Apply(model.Event{TimeMS: 26000, Type: model.EventSuppressedReport,
		Iface: "eth0", Group: "239.1.1.1", Member: "10.0.0.2"})
	v, ok := eng.Snapshot("eth0", "239.1.1.1")
	if !ok {
		t.Fatal("group missing after suppressed report")
	}
	if v.ExpiryMS != 235000 {
		t.Fatalf("suppressed report changed timer: expiry=%d, want 235000", v.ExpiryMS)
	}
	if v.SuppressedTotal != 1 {
		t.Fatalf("suppressed_total=%d, want 1", v.SuppressedTotal)
	}

	// First leave: group must survive with its timer untouched.
	eng.Apply(leave(40000, "239.1.1.1", "10.0.0.1"))
	v, ok = eng.Snapshot("eth0", "239.1.1.1")
	if !ok {
		t.Fatal("group deleted by a single leave with one member remaining")
	}
	if v.LastMember {
		t.Fatal("last_member set while a member remains")
	}
	if v.ExpiryMS != 235000 {
		t.Fatalf("leave changed timer: expiry=%d, want 235000", v.ExpiryMS)
	}
	if len(v.Members) != 1 || v.Members[0] != "10.0.0.2" {
		t.Fatalf("members=%v, want [10.0.0.2]", v.Members)
	}

	// Second (last) leave: last-member phase, deadline 50000+15000.
	eng.Apply(leave(50000, "239.1.1.1", "10.0.0.2"))
	v, ok = eng.Snapshot("eth0", "239.1.1.1")
	if !ok {
		t.Fatal("group deleted immediately on last leave; want last-member phase")
	}
	if !v.LastMember || v.ExpiryMS != 65000 {
		t.Fatalf("last-member state = (%v, %d), want (true, 65000)", v.LastMember, v.ExpiryMS)
	}

	eng.RunUntil(100000)
	if _, ok := eng.Snapshot("eth0", "239.1.1.1"); ok {
		t.Fatal("group survived last-member timeout")
	}
	tr := lastTransition(eng, engine.TGroupExpired)
	if tr == nil || tr.TimeMS != 65000 || tr.Reason != engine.ReasonLastMemberTimeout {
		t.Fatalf("expiry transition = %+v, want t=65000 reason=last_member_timeout", tr)
	}
	ivs := eng.Intervals()["eth0/239.1.1.1"]
	if len(ivs) != 1 || ivs[0].StartMS != 0 || ivs[0].EndMS == nil || *ivs[0].EndMS != 65000 {
		t.Fatalf("intervals=%+v, want [0,65000)", ivs)
	}
}

// TestLostQueryExpiresAtGMI: a query that gets no answer does not itself
// shorten membership; the group lives until last_refresh + GMI.
func TestLostQueryExpiresAtGMI(t *testing.T) {
	eng := newEngine(t)
	eng.Apply(report(0, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(generalQuery(100000)) // lost: no report follows

	eng.RunUntil(209999) // 1ms before GMI deadline: still alive
	if _, ok := eng.Snapshot("eth0", "239.1.1.1"); !ok {
		t.Fatal("group expired before its GMI deadline")
	}
	eng.RunUntil(210000) // exactly at the deadline: gone (inclusive expiry)
	if _, ok := eng.Snapshot("eth0", "239.1.1.1"); ok {
		t.Fatal("group alive at its GMI deadline")
	}
	tr := lastTransition(eng, engine.TGroupExpired)
	if tr == nil || tr.Reason != engine.ReasonMembershipTimeout || tr.TimeMS != 210000 {
		t.Fatalf("expiry = %+v, want t=210000 reason=membership_timeout", tr)
	}
}

// TestLeaveThenRejoin: a report arriving inside the last-member window
// rescues the group and re-arms the full GMI.
func TestLeaveThenRejoin(t *testing.T) {
	eng := newEngine(t)
	eng.Apply(report(0, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(leave(50000, "239.1.1.1", "10.0.0.1"))

	v, _ := eng.Snapshot("eth0", "239.1.1.1")
	if !v.LastMember || v.ExpiryMS != 65000 {
		t.Fatalf("after leave: (%v, %d), want (true, 65000)", v.LastMember, v.ExpiryMS)
	}

	eng.Apply(report(60000, "239.1.1.1", "10.0.0.1", nil)) // 5s before deadline
	tr := lastTransition(eng, engine.TReportAccepted)
	if tr == nil || tr.Reason != engine.ReasonRescuedLastMember {
		t.Fatalf("rescue transition = %+v, want reason=rescued_last_member", tr)
	}
	v, _ = eng.Snapshot("eth0", "239.1.1.1")
	if v.LastMember || v.ExpiryMS != 270000 {
		t.Fatalf("after rejoin: (%v, %d), want (false, 270000)", v.LastMember, v.ExpiryMS)
	}

	eng.RunUntil(300000)
	ivs := eng.Intervals()["eth0/239.1.1.1"]
	if len(ivs) != 1 || *ivs[0].EndMS != 270000 {
		t.Fatalf("intervals=%+v, want single [0,270000)", ivs)
	}
}

// TestBoundaryTimeoutExact: at exactly the expiry deadline the group is
// already gone; a report at that instant starts a NEW interval.
func TestBoundaryTimeoutExact(t *testing.T) {
	eng := newEngine(t)
	eng.Apply(report(0, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(report(210000, "239.1.1.1", "10.0.0.1", nil)) // exactly at deadline
	eng.RunUntil(500000)

	ivs := eng.Intervals()["eth0/239.1.1.1"]
	if len(ivs) != 2 {
		t.Fatalf("intervals=%+v, want two intervals", ivs)
	}
	if *ivs[0].EndMS != 210000 || ivs[1].StartMS != 210000 || *ivs[1].EndMS != 420000 {
		t.Fatalf("intervals=[%d,%d) [%d,%d), want [0,210000) [210000,420000)",
			ivs[0].StartMS, *ivs[0].EndMS, ivs[1].StartMS, *ivs[1].EndMS)
	}
}

// TestBoundaryTimeoutOneMSBefore: 1ms before the deadline the group is
// still alive and the report refreshes it.
func TestBoundaryTimeoutOneMSBefore(t *testing.T) {
	eng := newEngine(t)
	eng.Apply(report(0, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(report(209999, "239.1.1.1", "10.0.0.1", nil))
	eng.RunUntil(500000)

	ivs := eng.Intervals()["eth0/239.1.1.1"]
	if len(ivs) != 1 || *ivs[0].EndMS != 419999 {
		t.Fatalf("intervals=%+v, want single [0,419999)", ivs)
	}
}

// TestStaleReportDoesNotOverwriteNewRound: a late answer to query round 1
// arriving after round 2 started must not move the timer.
func TestStaleReportDoesNotOverwriteNewRound(t *testing.T) {
	eng := newEngine(t)
	eng.Apply(report(0, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(generalQuery(100000))                               // round 1
	eng.Apply(report(150000, "239.1.1.1", "10.0.0.1", genPtr(1))) // expiry 360000
	eng.Apply(generalQuery(200000))                               // round 2
	eng.Apply(report(205000, "239.1.1.1", "10.0.0.1", genPtr(1))) // stale

	v, _ := eng.Snapshot("eth0", "239.1.1.1")
	if v.ExpiryMS != 360000 {
		t.Fatalf("stale report moved timer: expiry=%d, want 360000", v.ExpiryMS)
	}
	if tr := lastTransition(eng, engine.TStaleReportIgnored); tr == nil || tr.Reason != engine.ReasonOlderThanCurrRound {
		t.Fatalf("missing stale_report_ignored transition")
	}

	eng.Apply(report(250000, "239.1.1.1", "10.0.0.1", genPtr(2))) // current round
	v, _ = eng.Snapshot("eth0", "239.1.1.1")
	if v.ExpiryMS != 460000 {
		t.Fatalf("current-round report: expiry=%d, want 460000", v.ExpiryMS)
	}
}

// TestStaleReportDoesNotResurrect: a stale report for an expired group
// must not recreate it.
func TestStaleReportDoesNotResurrect(t *testing.T) {
	eng := newEngine(t)
	eng.Apply(report(0, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(generalQuery(100000))                               // round 1
	eng.RunUntil(210000)                                          // group expires
	eng.Apply(report(220000, "239.1.1.1", "10.0.0.1", genPtr(0))) // stale

	if _, ok := eng.Snapshot("eth0", "239.1.1.1"); ok {
		t.Fatal("stale report resurrected an expired group")
	}
	ivs := eng.Intervals()["eth0/239.1.1.1"]
	if len(ivs) != 1 || *ivs[0].EndMS != 210000 {
		t.Fatalf("intervals=%+v, want single [0,210000)", ivs)
	}
}

// TestRejectionCategories: each malformed event is rejected with its
// specific failure category and changes no state.
func TestRejectionCategories(t *testing.T) {
	eng := newEngine(t)
	cases := []struct {
		ev   model.Event
		want string
	}{
		{model.Event{TimeMS: 0, Type: model.EventReport, Iface: "eth9", Group: "239.1.1.1", Member: "10.0.0.1"}, cats.UnknownInterface},
		{model.Event{TimeMS: 0, Type: model.EventReport, Iface: "eth0", Group: "8.8.8.8", Member: "10.0.0.1"}, cats.InvalidGroup},
		{model.Event{TimeMS: 0, Type: model.EventReport, Iface: "eth0", Group: "239.1.1.1", Member: "not-an-ip"}, cats.InvalidMember},
		{model.Event{TimeMS: 0, Type: model.EventLeave, Iface: "eth0", Group: "239.1.1.1", Member: "10.0.0.1"}, cats.UnknownMember},
		{model.Event{TimeMS: 0, Type: "nonsense", Iface: "eth0"}, cats.BadEvent},
		{model.Event{TimeMS: -5, Type: model.EventReport, Iface: "eth0", Group: "239.1.1.1", Member: "10.0.0.1"}, cats.BadEvent},
	}
	for i, c := range cases {
		eng.Apply(c.ev)
		got := eng.Rejections()
		if len(got) != i+1 {
			t.Fatalf("case %d: rejections=%d, want %d", i, len(got), i+1)
		}
		if got[i].Category != c.want {
			t.Fatalf("case %d: category=%s, want %s", i, got[i].Category, c.want)
		}
		if got[i].EventIndex != i {
			t.Fatalf("case %d: event_index=%d, want %d", i, got[i].EventIndex, i)
		}
	}
	if len(eng.Transitions()) != 0 {
		t.Fatalf("rejected events produced %d transitions, want 0", len(eng.Transitions()))
	}
	if len(eng.FinalGroups()) != 0 {
		t.Fatal("rejected events created group state")
	}
}

// TestOutOfOrderAndFutureGeneration cover the two time-related categories.
func TestOutOfOrderAndFutureGeneration(t *testing.T) {
	eng := newEngine(t)
	eng.Apply(report(0, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(report(100, "239.1.1.1", "10.0.0.1", nil))
	eng.Apply(report(50, "239.1.1.1", "10.0.0.1", nil))        // backwards
	eng.Apply(report(200, "239.1.1.1", "10.0.0.1", genPtr(7))) // future round

	got := eng.Rejections()
	if len(got) != 2 || got[0].Category != cats.OutOfOrder || got[1].Category != cats.FutureGeneration {
		t.Fatalf("rejections=%+v, want [out_of_order, future_generation]", got)
	}
}
