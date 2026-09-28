package snapshot_test

import (
	"context"
	"testing"

	"clsnap/internal/apperr"
	"clsnap/internal/clock"
	"clsnap/internal/kernel"
	"clsnap/internal/protocol"
	"clsnap/internal/snapshot"
	"clsnap/internal/store"
	"clsnap/internal/transport"
)

type router map[string]protocol.NodeID

func (r router) OwnerOf(a string) (protocol.NodeID, bool) { n, ok := r[a]; return n, ok }

// fixture builds three coordinators over one DirectTransport + per-node
// memory stores, enough to drive the protocol state machine by hand.
type fixture struct {
	t       *testing.T
	nodes   []protocol.NodeID
	stores  map[protocol.NodeID]*store.Memory
	coords  map[protocol.NodeID]*snapshot.Coordinator
	dir     *transport.DirectTransport
	rt      router
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	nodes := []protocol.NodeID{"n1", "n2", "n3"}
	seed := map[protocol.NodeID][]protocol.Account{
		"n1": {{ID: "a", Owner: "n1", Balance: 100}},
		"n2": {{ID: "d", Owner: "n2", Balance: 100}},
		"n3": {{ID: "g", Owner: "n3", Balance: 100}},
	}
	rt := router{"a": "n1", "d": "n2", "g": "n3"}
	dir := transport.NewDirect(nodes)
	f := &fixture{t: t, nodes: nodes, stores: map[protocol.NodeID]*store.Memory{},
		coords: map[protocol.NodeID]*snapshot.Coordinator{}, dir: dir, rt: rt}
	ctx := context.Background()
	for _, n := range nodes {
		st := store.NewMemory()
		if err := st.Bootstrap(ctx, n, seed[n]); err != nil {
			t.Fatal(err)
		}
		if _, err := st.BumpEpoch(ctx, n); err != nil {
			t.Fatal(err)
		}
		led, err := kernel.NewLedger(n, seed[n])
		if err != nil {
			t.Fatal(err)
		}
		peers := []protocol.NodeID{}
		for _, p := range nodes {
			if p != n {
				peers = append(peers, p)
			}
		}
		c, err := snapshot.New(snapshot.Deps{
			Node: n, Peers: peers, Ledger: led, Store: st, Transport: dir,
			Clock: clock.New(), Router: rt, Epoch: 1,
		})
		if err != nil {
			t.Fatal(err)
		}
		dir.RegisterReceiver(n, recv{c})
		f.stores[n] = st
		f.coords[n] = c
	}
	return f
}

type recv struct{ c *snapshot.Coordinator }

func (r recv) Receive(ctx context.Context, e protocol.Envelope) error { return r.c.Receive(ctx, e) }

func (f *fixture) pumpAll(src, dst protocol.NodeID) {
	t := f.t
	if _, err := f.dir.PumpAll(context.Background(), src, dst); err != nil {
		t.Fatalf("pump %s->%s: %v", src, dst, err)
	}
}

func (f *fixture) flushAll() {
	for _, s := range f.nodes {
		for _, d := range f.nodes {
			if s == d {
				continue
			}
			if _, err := f.coords[s].FlushPeer(context.Background(), d); err != nil {
				f.t.Fatalf("flush %s->%s: %v", s, d, err)
			}
		}
	}
}

func (f *fixture) deliverAll() {
	// Repeat (flush all outboxes, deliver one envelope per channel) until no
	// channel has anything pending. Six directed channels over three nodes
	// converge in well under 50 rounds for these tests.
	for i := 0; i < 50; i++ {
		f.flushAll()
		any := false
		for _, s := range f.nodes {
			for _, d := range f.nodes {
				if s == d {
					continue
				}
				if f.dir.Pending(s, d) > 0 {
					any = true
					if _, err := f.dir.Pump(context.Background(), s, d, 1); err != nil {
						f.t.Fatalf("pump %s->%s: %v", s, d, err)
					}
				}
			}
		}
		if !any {
			return
		}
	}
	f.t.Fatal("deliverAll did not converge")
}

// TestInitiateRecordsLocalAndCompletesAllNodes is the textbook protocol run
// with no in-flight messages: every node records the seed balance 100.
func TestInitiateRecordsLocalAndCompletesAllNodes(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	if err := f.coords["n1"].Initiate(ctx, "S1"); err != nil {
		t.Fatal(err)
	}
	f.flushAll()
	f.deliverAll()

	for _, n := range f.nodes {
		rec, err := f.coords[n].Record(ctx, "S1")
		if err != nil {
			t.Fatalf("record %s: %v", n, err)
		}
		if rec.Phase != protocol.PhaseComplete {
			t.Fatalf("node %s phase %s, want complete", n, rec.Phase)
		}
		if rec.Local.Total != 100 {
			t.Fatalf("node %s local total %d, want 100", n, rec.Local.Total)
		}
		for from, ch := range rec.Channels {
			if !ch.Closed {
				t.Fatalf("channel %s->%s not closed", from, n)
			}
			if len(ch.Messages) != 0 {
				t.Fatalf("channel %s->%s captured %d messages, want 0", from, n, len(ch.Messages))
			}
		}
	}
}

// TestDuplicateInitiateClassified ensures a second Initiate of the same id is
// a state_conflict, not a fresh round.
func TestDuplicateInitiateClassified(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	if err := f.coords["n1"].Initiate(ctx, "S1"); err != nil {
		t.Fatal(err)
	}
	err := f.coords["n1"].Initiate(ctx, "S1")
	if !apperr.IsKind(err, apperr.KindConflict) {
		t.Fatalf("duplicate initiate: %v", err)
	}
}

// TestMarkerBeforeAnyMessageClosesEmpty is the core "marker closes channel"
// rule: with the marker arriving first, even a later message on that channel
// must NOT be captured.
func TestMarkerFirstClosesEmpty(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	// n1 initiates and sends marker to n2, delivered before any transfer.
	if err := f.coords["n1"].Initiate(ctx, "S1"); err != nil {
		t.Fatal(err)
	}
	f.flushAll()
	f.pumpAll("n1", "n2") // n2 records, channel n1->n2 closed empty

	// Now send a transfer n1->n2 AFTER the channel closed.
	if err := f.coords["n1"].StartTransfer(ctx, protocol.Transfer{
		Ref: "late", From: "a", To: "d", Amount: 7,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.coords["n1"].FlushPeer(ctx, "n2"); err != nil {
		t.Fatal(err)
	}
	f.pumpAll("n1", "n2")

	rec, err := f.coords["n2"].Record(ctx, "S1")
	if err != nil {
		t.Fatal(err)
	}
	ch := rec.Channels["n1"]
	if !ch.Closed || len(ch.Messages) != 0 || ch.Sum != 0 {
		t.Fatalf("post-marker message captured: closed=%v msgs=%d sum=%d",
			ch.Closed, len(ch.Messages), ch.Sum)
	}
	// The credit still lands on the live ledger (the protocol does not
	// pause) — verify via the n2 coordinator's stored record balance delta
	// indirectly through a subsequent transfer room, kept simple here: the
	// live ledger total is asserted in the scenario integration tests.
}

// TestMessageBeforeMarkerIsCaptured verifies the complementary rule: a
// message delivered to a recording node before that channel's marker is
// captured as channel state.
func TestMessageBeforeMarkerIsCaptured(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()

	// n2 initiates: it records local state and has n3->n2 OPEN, waiting for
	// n3's marker.
	if err := f.coords["n2"].Initiate(ctx, "S1"); err != nil {
		t.Fatal(err)
	}
	f.flushAll()

	// n3 has NOT heard of S1 yet, but it can still transfer. The message
	// reaches n2 while n3->n2 is open -> it must be captured.
	if err := f.coords["n3"].StartTransfer(ctx, protocol.Transfer{
		Ref: "early3", From: "g", To: "d", Amount: 11,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.coords["n3"].FlushPeer(ctx, "n2"); err != nil {
		t.Fatal(err)
	}
	f.pumpAll("n3", "n2")

	// Now n3 receives n2's marker: it records for S1 and emits its own
	// marker, which then closes n3->n2 at n2 with [early3] captured.
	f.pumpAll("n2", "n3")
	f.flushAll()
	f.pumpAll("n3", "n2")

	rec, err := f.coords["n2"].Record(ctx, "S1")
	if err != nil {
		t.Fatal(err)
	}
	ch := rec.Channels["n3"]
	if !ch.Closed {
		t.Fatal("n3->n2 not closed")
	}
	if len(ch.Messages) != 1 || ch.Messages[0].Ref != "early3" || ch.Sum != 11 {
		t.Fatalf("n3->n2 capture = %+v, want [early3:11]", ch.Messages)
	}
	// The captured message also credited the live ledger (capture happens
	// before credit, but both occur on delivery).
	if got, _ := f.coords["n2"].Record(ctx, "S1"); got.Local.Accounts["d"].Balance != 100 {
		// local snapshot balance stays at record time 100
		t.Fatalf("snapshot local d mutated: %d", got.Local.Accounts["d"].Balance)
	}
}

// TestStaleEpochRejected checks the restart guard: a marker carrying an old
// boot epoch is a state_conflict/round_stale_after_restart.
func TestStaleEpochRejected(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	stale := protocol.Envelope{
		Type: protocol.MsgMarker, Src: "n1", Dst: "n2", Lamport: 1,
		Marker: &protocol.Marker{Snapshot: "OLD", Epoch: 1},
	}
	// n2 is epoch 1 in the fixture; simulate restart by building an epoch-2
	// coordinator on the SAME durable store.
	st := f.stores["n2"]
	// Mark an OLD round recording so there is durable state to abort.
	if err := st.SaveRecord(ctx, protocol.NodeRecord{
		Node: "n2", Snapshot: "OLD", Phase: protocol.PhaseRecording,
		Channels: map[protocol.NodeID]*protocol.ChannelState{
			"n1": {From: "n1", To: "n2", Snapshot: "OLD", Messages: []protocol.Transfer{}},
			"n3": {From: "n3", To: "n2", Snapshot: "OLD", Messages: []protocol.Transfer{}},
		},
	}); err != nil {
		t.Fatal(err)
	}
	newEpoch, err := st.BumpEpoch(ctx, "n2")
	if err != nil || newEpoch != 2 {
		t.Fatalf("bump epoch = %d, %v", newEpoch, err)
	}
	led, _ := kernel.NewLedger("n2", []protocol.Account{{ID: "d", Owner: "n2", Balance: 100}})
	c2, err := snapshot.New(snapshot.Deps{
		Node: "n2", Peers: []protocol.NodeID{"n1", "n3"}, Ledger: led, Store: st,
		Transport: f.dir, Clock: clock.New(), Router: f.rt, Epoch: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	aborted, err := c2.RecoverAbortedRounds(ctx)
	if err != nil || len(aborted) != 1 || aborted[0] != "OLD" {
		t.Fatalf("recover = %v, %v", aborted, err)
	}
	rec, _ := st.GetRecord(ctx, "n2", "OLD")
	if rec.Phase != protocol.PhaseAborted {
		t.Fatalf("OLD phase %s, want aborted", rec.Phase)
	}
	// Re-deliver a stale marker; it must be rejected as a conflict.
	err = c2.Receive(ctx, stale)
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindConflict || ae.Code != apperr.CodeRoundStale {
		t.Fatalf("stale marker: %v", err)
	}
}

// TestParallelRoundsIsolated runs S1 and S2 concurrently and asserts the two
// rounds' records on one node are independent.
func TestParallelRoundsIsolated(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	if err := f.coords["n1"].Initiate(ctx, "S1"); err != nil {
		t.Fatal(err)
	}
	f.flushAll()
	// Before S1 marker reaches n2, n2 starts S2.
	if err := f.coords["n2"].Initiate(ctx, "S2"); err != nil {
		t.Fatal(err)
	}
	f.flushAll()
	f.deliverAll()

	r1, err := f.coords["n3"].Record(ctx, "S1")
	if err != nil {
		t.Fatal(err)
	}
	r2, err := f.coords["n3"].Record(ctx, "S2")
	if err != nil {
		t.Fatal(err)
	}
	if r1.Phase != protocol.PhaseComplete || r2.Phase != protocol.PhaseComplete {
		t.Fatalf("phases S1=%s S2=%s", r1.Phase, r2.Phase)
	}
	if r1.Local.Snapshot != "S1" || r2.Local.Snapshot != "S2" {
		t.Fatalf("records not isolated by snapshot id: %s / %s",
			r1.Local.Snapshot, r2.Local.Snapshot)
	}
}

// TestTooManyRoundsExhausted verifies the resource-exhaustion category.
func TestTooManyRoundsExhausted(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	for i := 0; i < snapshot.MaxConcurrentRounds; i++ {
		id := protocol.SnapshotID("R" + string(rune('A'+i/26)) + string(rune('A'+i%26)))
		if err := f.coords["n1"].Initiate(ctx, id); err != nil {
			t.Fatalf("round %d: %v", i, err)
		}
	}
	err := f.coords["n1"].Initiate(ctx, "OVERFLOW")
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindExhausted || ae.Code != apperr.CodeTooManySnapshots {
		t.Fatalf("overflow: %v", err)
	}
}
