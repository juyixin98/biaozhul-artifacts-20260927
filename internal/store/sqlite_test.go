package store

import (
	"testing"

	"igmpv2timer/internal/model"
)

func TestEventJournalRoundTrip(t *testing.T) {
	st, err := Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	evs := []model.Event{
		{Seq: 1, At: 100, Kind: model.EvReport, Iface: "eth0",
			Group: "239.1.2.3", Member: "a", SourceAddr: "192.0.2.1"},
		{Seq: 2, At: 200, Kind: model.EvLeave, Iface: "eth0",
			Group: "239.1.2.3", Member: "a", SourceAddr: "192.0.2.1"},
	}
	for _, ev := range evs {
		if err := st.AppendEvent(ev); err != nil {
			t.Fatal(err)
		}
	}
	got, err := st.Events()
	if err != nil || len(got) != 2 {
		t.Fatalf("events=%d err=%v", len(got), err)
	}
	if got[0].Seq != 1 || got[1].Group != "239.1.2.3" {
		t.Errorf("order/content wrong: %+v", got)
	}
}

func TestDiagAndEmittedAndInterval(t *testing.T) {
	st, _ := Open(":memory:")
	defer st.Close()

	if err := st.AppendDiag(model.Diag{Seq: 1, At: 5, Iface: "eth0",
		Verdict: model.VTimeout, Reason: "x"}); err != nil {
		t.Fatal(err)
	}
	if err := st.AppendEmitted(model.EmittedPkt{At: 7, Iface: "eth0",
		Packet: model.PktQueryGeneral, Gen: 3}); err != nil {
		t.Fatal(err)
	}
	iv := model.Interval{Iface: "eth0", Group: "g", Start: 1, End: 0}
	if err := st.UpsertInterval(iv); err != nil {
		t.Fatal(err)
	}
	iv.End = 9
	iv.Reason = "done"
	if err := st.UpsertInterval(iv); err != nil {
		t.Fatal(err)
	}

	d, _ := st.Diagnostics()
	if len(d) != 1 || d[0].Verdict != model.VTimeout {
		t.Errorf("diags=%v", d)
	}
	p, _ := st.Emitted()
	if len(p) != 1 || p[0].Gen != 3 || int64(p[0].At) != 7 {
		t.Errorf("emitted=%v", p)
	}
	ivs, _ := st.Intervals()
	if len(ivs) != 1 || int64(ivs[0].End) != 9 || ivs[0].Reason != "done" {
		t.Errorf("intervals=%v", ivs)
	}

	if err := st.Reset(); err != nil {
		t.Fatal(err)
	}
	if evs, _ := st.Events(); len(evs) != 0 {
		t.Error("reset did not clear events")
	}
}

func TestOpenFileCreatesSchema(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/nested/igmp.sqlite"
	st, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	_ = st.AppendEvent(model.Event{Seq: 1, At: 1, Kind: model.EvReport, Iface: "e"})
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	// reopen: schema is idempotent and data persists.
	st2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	evs, err := st2.Events()
	if err != nil || len(evs) != 1 {
		t.Fatalf("persisted events=%d err=%v", len(evs), err)
	}
}
