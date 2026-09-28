package store_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"natlab/internal/model"
	"natlab/internal/store"
)

func openTemp(t *testing.T) *store.SQLiteStore {
	t.Helper()
	st, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

func t0() time.Time { return time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC) }

func TestInsertAndUniqueConstraints(t *testing.T) {
	st := openTemp(t)
	ctx := context.Background()
	m := &model.Mapping{
		RunID: "r", Protocol: model.UDP, SrcIP: "10.0.0.1", SrcPort: 1,
		DstIP: "1.1.1.1", DstPort: 53, MappedPort: 40000, State: model.StateOpen,
		CreatedAt: t0(), LastUsedAt: t0(), ExpiresAt: t0().Add(time.Minute),
	}
	if _, err := st.InsertMapping(ctx, "r", m); err != nil {
		t.Fatalf("insert: %v", err)
	}
	// Same active flow key must be rejected by the partial unique index.
	dup := *m
	dup.MappedPort = 40001
	if _, err := st.InsertMapping(ctx, "r", &dup); err == nil {
		t.Fatal("duplicate active flow inserted; unique index failed")
	}
	// Same active external port must be rejected too.
	other := &model.Mapping{
		RunID: "r", Protocol: model.UDP, SrcIP: "10.0.0.2", SrcPort: 2,
		DstIP: "1.1.1.2", DstPort: 53, MappedPort: 40000, State: model.StateOpen,
		CreatedAt: t0(), LastUsedAt: t0(), ExpiresAt: t0().Add(time.Minute),
	}
	if _, err := st.InsertMapping(ctx, "r", other); err == nil {
		t.Fatal("duplicate active port inserted; unique index failed")
	}
}

func TestSweepAndActiveLookup(t *testing.T) {
	st := openTemp(t)
	ctx := context.Background()
	mk := func(idSuffix int, port uint16, expires time.Time) *model.Mapping {
		return &model.Mapping{
			RunID: "r", Protocol: model.UDP, SrcIP: "10.0.0.1", SrcPort: uint16(1000 + idSuffix),
			DstIP: "1.1.1.1", DstPort: 53, MappedPort: port, State: model.StateOpen,
			CreatedAt: t0(), LastUsedAt: t0(), ExpiresAt: expires,
		}
	}
	alive, err := st.InsertMapping(ctx, "r", mk(1, 40000, t0().Add(60*time.Second)))
	if err != nil {
		t.Fatal(err)
	}
	dead, err := st.InsertMapping(ctx, "r", mk(2, 40001, t0().Add(10*time.Second)))
	if err != nil {
		t.Fatal(err)
	}

	now := t0().Add(20 * time.Second)
	closed, err := st.SweepExpired(ctx, "r", now)
	if err != nil {
		t.Fatal(err)
	}
	if len(closed) != 1 || closed[0].ID != dead {
		t.Fatalf("swept=%v, want id %d", closed, dead)
	}
	got, err := st.ActiveByFlow(ctx, "r",
		model.FlowKey{Protocol: model.UDP, SrcIP: "10.0.0.1", SrcPort: 1002, DstIP: "1.1.1.1", DstPort: 53}, now)
	if err != nil {
		t.Fatal(err)
	}
	if got != nil {
		t.Fatalf("expired flow still returned as active: %+v", got)
	}
	got2, err := st.ActiveByExtPort(ctx, "r", model.UDP, 40000, now)
	if err != nil || got2.ID != alive {
		t.Fatalf("alive mapping lookup: %v %+v", err, got2)
	}
	// Boundary: expires exactly at now counts as expired (<= sweep).
	boundary, _ := st.InsertMapping(ctx, "r", mk(3, 40002, t0().Add(40*time.Second)))
	closed2, _ := st.SweepExpired(ctx, "r", t0().Add(40*time.Second))
	if len(closed2) != 1 || closed2[0].ID != boundary {
		t.Fatalf("boundary sweep=%v, want id %d", closed2, boundary)
	}
}

func TestPortReusableAfterSweep(t *testing.T) {
	st := openTemp(t)
	ctx := context.Background()
	m := &model.Mapping{
		RunID: "r", Protocol: model.UDP, SrcIP: "10.0.0.1", SrcPort: 1,
		DstIP: "1.1.1.1", DstPort: 53, MappedPort: 40000, State: model.StateOpen,
		CreatedAt: t0(), LastUsedAt: t0(), ExpiresAt: t0().Add(10 * time.Second),
	}
	if _, err := st.InsertMapping(ctx, "r", m); err != nil {
		t.Fatal(err)
	}
	if _, err := st.SweepExpired(ctx, "r", t0().Add(20*time.Second)); err != nil {
		t.Fatal(err)
	}
	// Same flow and same port may now be re-inserted (old row is closed).
	m2 := *m
	m2.ExpiresAt = t0().Add(30 * time.Second)
	if _, err := st.InsertMapping(ctx, "r", &m2); err != nil {
		t.Fatalf("reuse after close should succeed: %v", err)
	}
}

func TestEventRoundTrip(t *testing.T) {
	st := openTemp(t)
	ctx := context.Background()
	pkt := model.Packet{
		Seq: 7, ObservedAt: t0(), Direction: model.Outbound,
		FiveTuple: model.FiveTuple{SrcIP: "10.0.0.1", SrcPort: 1, DstIP: "1.1.1.1", DstPort: 53, Protocol: model.TCP},
		Flags:     "SYN",
	}
	ev := &model.Event{
		RunID: "r", Seq: 7, ObservedAt: t0(), EffectiveAt: t0(),
		Packet: pkt, Accepted: true, Code: "", Reason: "",
		MappedPort: 40000, State: model.StateSynSent, Detail: `{"active_count":1}`,
	}
	if err := st.AppendEvent(ctx, ev); err != nil {
		t.Fatal(err)
	}
	got, err := st.ListEvents(ctx, "r", 0)
	if err != nil || len(got) != 1 {
		t.Fatalf("events=%v err=%v", got, err)
	}
	g := got[0]
	if g.Seq != 7 || !g.Accepted || g.MappedPort != 40000 || g.State != model.StateSynSent {
		t.Fatalf("event fields=%+v", g)
	}
	if g.Packet.FiveTuple.SrcIP != "10.0.0.1" || g.Packet.Flags != "SYN" ||
		g.Packet.FiveTuple.Protocol != model.TCP {
		t.Fatalf("packet round-trip=%+v", g.Packet)
	}
}

func TestEnsureRunClockPersistence(t *testing.T) {
	st := openTemp(t)
	ctx := context.Background()
	hw, err := st.EnsureRun(ctx, "rr")
	if err != nil || !hw.IsZero() {
		t.Fatalf("new run highwater=%v err=%v", hw, err)
	}
	want := t0().Add(5 * time.Second)
	if err := st.SetClock(ctx, "rr", want); err != nil {
		t.Fatal(err)
	}
	hw2, err := st.EnsureRun(ctx, "rr")
	if err != nil || !hw2.Equal(want) {
		t.Fatalf("persisted highwater=%v err=%v want %v", hw2, err, want)
	}
}
