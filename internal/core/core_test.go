package core_test

import (
	"testing"

	"igmpv2timer/internal/clock"
	"igmpv2timer/internal/config"
	"igmpv2timer/internal/core"
	"igmpv2timer/internal/model"
)

func testCore(t *testing.T, timing model.Millis) (*core.Core, *clock.Clock) {
	t.Helper()
	cfg := config.Default()
	cfg.HTTPAddr = "127.0.0.1:0"
	cfg.Timing.QueryInterval = 1_000_000
	cfg.Timing.QueryResponseInterval = 200
	cfg.Timing.GroupMembershipInterval = int64(timing)
	cfg.Timing.LastMemberQueryInterval = 100
	cfg.Timing.LastMemberQueryCount = 2
	clk := clock.New()
	c, err := core.New(cfg, clk)
	if err != nil {
		t.Fatal(err)
	}
	return c, clk
}

func report(iface, group, member, addr string, at model.Millis, resp string) model.Event {
	return model.Event{At: at, Kind: model.EvReport, Iface: iface, Group: group,
		Member: member, SourceAddr: addr, ResponseTo: resp}
}

func leave(iface, group, member, addr string, at model.Millis) model.Event {
	return model.Event{At: at, Kind: model.EvLeave, Iface: iface, Group: group,
		Member: member, SourceAddr: addr}
}

func advance(t *testing.T, c *core.Core, ms model.Millis) {
	t.Helper()
	if _, _, err := c.Tick(ms); err != nil {
		t.Fatal(err)
	}
}

func TestReportCreatesRefreshesAndTimesOut(t *testing.T) {
	c, _ := testCore(t, 300)
	advance(t, c, 100)
	d := c.Apply(report("eth0", "239.1.1.1", "a", "192.0.2.1", 100, ""))
	if d.Verdict != model.VAccepted || d.Reason != model.ReasonMembershipCreated {
		t.Fatalf("create verdict=%s reason=%s", d.Verdict, d.Reason)
	}
	if d.MembershipDeadline != 400 {
		t.Errorf("deadline=%d want 400", d.MembershipDeadline)
	}

	advance(t, c, 200)
	d = c.Apply(report("eth0", "239.1.1.1", "b", "192.0.2.2", 200, ""))
	if d.Reason != model.ReasonMembershipRefreshed || d.MembershipDeadline != 500 {
		t.Errorf("refresh deadline=%d want 500 (%s)", d.MembershipDeadline, d.Reason)
	}

	// present at 499, gone exactly at 500.
	advance(t, c, 499)
	if len(c.Snapshot().Groups) != 1 {
		t.Fatal("group should be present at 499")
	}
	advance(t, c, 500)
	if len(c.Snapshot().Groups) != 0 {
		t.Fatalf("group must time out at 500, got %d groups", len(c.Snapshot().Groups))
	}
	iv := c.Intervals()
	if len(iv) != 1 || int64(iv[0].Start) != 100 || int64(iv[0].End) != 500 ||
		iv[0].Reason != model.ReasonMembershipTimeout {
		t.Errorf("interval=%v want 100..500 membership_interval_expired", iv)
	}
}

func TestRejectCategories(t *testing.T) {
	c, _ := testCore(t, 300)
	advance(t, c, 10)

	// unknown interface
	d := c.Apply(report("nope", "239.1.1.1", "a", "192.0.2.1", 10, ""))
	if d.Verdict != model.VRejected || d.Reason != "unknown_interface" {
		t.Errorf("iface verdict=%s/%s", d.Verdict, d.Reason)
	}
	// non-multicast group
	d = c.Apply(report("eth0", "10.0.0.1", "a", "192.0.2.1", 10, ""))
	if d.Verdict != model.VRejected || d.Reason != model.ReasonBadGroupAddr {
		t.Errorf("group verdict=%s/%s", d.Verdict, d.Reason)
	}
	// link-local control group
	d = c.Apply(report("eth0", "224.0.0.1", "a", "192.0.2.1", 10, ""))
	if d.Reason != model.ReasonBadGroupAddr {
		t.Errorf("224.0.0.1 verdict=%s/%s", d.Verdict, d.Reason)
	}
	// bad source
	d = c.Apply(report("eth0", "239.1.1.1", "a", "0.0.0.0", 10, ""))
	if d.Verdict != model.VRejected || d.Reason != model.ReasonBadSourceAddr {
		t.Errorf("src verdict=%s/%s", d.Verdict, d.Reason)
	}
	// leave for absent group
	d = c.Apply(leave("eth0", "239.1.1.1", "a", "192.0.2.1", 10))
	if d.Verdict != model.VRejected || d.Reason != model.ReasonLeaveNoGroup {
		t.Errorf("leave-none verdict=%s/%s", d.Verdict, d.Reason)
	}

	// valid join, then leave from a non-member -> rejected (group retained)
	advance(t, c, 20)
	c.Apply(report("eth0", "239.1.1.1", "a", "192.0.2.1", 20, ""))
	d = c.Apply(leave("eth0", "239.1.1.1", "ghost", "192.0.2.9", 30))
	if d.Verdict != model.VRejected || d.Reason != model.ReasonLeaveUnknownMember {
		t.Errorf("unknown member verdict=%s/%s", d.Verdict, d.Reason)
	}
}

func TestFutureGenerationIsUndecidable(t *testing.T) {
	c, _ := testCore(t, 300)
	advance(t, c, 100)
	// report claiming to answer gen 5 when no query has been sent
	d := c.Apply(report("eth0", "239.1.1.1", "a", "192.0.2.1", 100, "5"))
	if d.Verdict != model.VUndecidable || d.Reason != model.ReasonGenerationUnknown {
		t.Fatalf("future gen verdict=%s/%s", d.Verdict, d.Reason)
	}
	if len(c.Snapshot().Groups) != 0 {
		t.Error("undecidable report must not create state")
	}
}

func TestStaleRoundDoesNotRecreateGroup(t *testing.T) {
	c, _ := testCore(t, 300)
	advance(t, c, 100)
	c.Apply(report("eth0", "239.1.1.1", "a", "192.0.2.1", 100, ""))
	advance(t, c, 100)
	if _, _, err := c.InjectGeneralQuery("eth0", "g1"); err != nil {
		t.Fatal(err)
	}
	advance(t, c, 400) // group times out at 400
	if _, d2, err := c.InjectGeneralQuery("eth0", "g2"); err != nil {
		t.Fatal(err)
	} else if d2.GenActive != 2 {
		t.Fatalf("gen2 not opened: %d", d2.GenActive)
	}
	// late answer to gen 1 after group expired
	d := c.Apply(report("eth0", "239.1.1.1", "a", "192.0.2.1", 450, "1"))
	if d.Verdict != model.VStale || d.GenActive != 2 || d.GenApplied != 1 {
		t.Fatalf("stale verdict=%s gens=%d/%d", d.Verdict, d.GenActive, d.GenApplied)
	}
	if len(c.Snapshot().Groups) != 0 {
		t.Error("stale round report recreated the group")
	}
}

func TestLMQGenerationGuardWhitebox(t *testing.T) {
	c, _ := testCore(t, 100000)
	advance(t, c, 100)
	c.Apply(report("eth0", "239.1.1.1", "a", "192.0.2.1", 100, ""))
	advance(t, c, 200)
	d := c.Apply(leave("eth0", "239.1.1.1", "a", "192.0.2.1", 200))
	if d.Reason != model.ReasonLastMemberStarted {
		t.Fatalf("leave reason=%s", d.Reason)
	}
	// GSQ1 at 300, GSQ2 at 400, deletion deadline 400; but a report at 350
	// opens a new membership generation and cancels the LMQ.
	advance(t, c, 300)
	advance(t, c, 350)
	rj := c.Apply(report("eth0", "239.1.1.1", "a", "192.0.2.1", 350, ""))
	if rj.Reason != model.ReasonReportDuringLMQ {
		t.Fatalf("rejoin reason=%s", rj.Reason)
	}
	// advancing well past the OLD deadline (400) must not delete the group
	advance(t, c, 5000)
	if len(c.Snapshot().Groups) != 1 {
		t.Fatal("generation guard failed: old LMQ round deleted fresh group")
	}
}

func TestOneLeaveAmongManyRetainsGroup(t *testing.T) {
	c, _ := testCore(t, 100000)
	advance(t, c, 0)
	c.Apply(report("eth0", "239.1.1.1", "a", "192.0.2.1", 0, ""))
	advance(t, c, 10)
	c.Apply(report("eth0", "239.1.1.1", "b", "192.0.2.2", 10, ""))
	d := c.Apply(leave("eth0", "239.1.1.1", "a", "192.0.2.1", 20))
	if d.Reason != "member_left_group_retained" {
		t.Fatalf("reason=%s", d.Reason)
	}
	if len(d.Members) != 1 || d.Members[0] != "b" {
		t.Errorf("members=%v", d.Members)
	}
	emitted, _, _ := c.Tick(1000)
	for _, p := range emitted {
		if p.Packet == model.PktQueryGroup {
			t.Error("no group-specific query may be sent while a member remains")
		}
	}
}

func TestClockRejectsBackwardTick(t *testing.T) {
	c, _ := testCore(t, 300)
	advance(t, c, 100)
	if _, _, err := c.Tick(50); err == nil {
		t.Fatal("backward tick must error")
	}
}
