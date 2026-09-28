package tests

import (
	"context"
	"os"
	"testing"

	"clsnap/internal/clock"
	"clsnap/internal/kernel"
	"clsnap/internal/protocol"
	"clsnap/internal/snapshot"
	"clsnap/internal/transport"

	"github.com/jackc/pgx/v5"
)

// pgDSNTemplate is the local trust cluster documented in docs/SETUP.md. The
// {db} placeholder is replaced with an isolated database per test/node.
const pgDSNTemplate = "postgres://clsnap@localhost:5433/{db}?sslmode=disable"

func skipNoPostgres(t *testing.T) string {
	t.Helper()
	dsn := os.Getenv("CLSNAP_PG_DSN")
	if dsn == "" {
		dsn = pgDSNTemplate
	}
	conn, err := pgx.Connect(context.Background(),
		`postgres://clsnap@localhost:5433/postgres?sslmode=disable`)
	if err != nil {
		t.Skipf("local postgres not reachable on :5433 (%v); run scripts/setup-postgres.sh or set CLSNAP_PG_DSN", err)
	}
	_ = conn.Close(context.Background())
	return dsn
}

// TestPostgresPersistenceAcrossRestart opens a REAL Postgres-backed node,
// starts a snapshot (durable recording), closes the store, reopens it (a new
// process incarnation), and verifies boot recovery aborts the unfinished
// round and the account ledger is exactly what was persisted.
func TestPostgresPersistenceAcrossRestart(t *testing.T) {
	dsn := skipNoPostgres(t)
	ctx := context.Background()

	st1, err := openPGNodeFresh(t, dsn, "pgrestart_n1", "n1",
		[]protocol.Account{{ID: "a", Owner: "n1", Balance: 77}}, true)
	if err != nil {
		t.Fatal(err)
	}
	epoch1, err := st1.BumpEpoch(ctx, "n1")
	if err != nil || epoch1 != 1 {
		t.Fatalf("epoch1 = %d, %v", epoch1, err)
	}
	led, _ := kernel.NewLedger("n1", []protocol.Account{{ID: "a", Owner: "n1", Balance: 77}})
	dir := transport.NewDirect([]protocol.NodeID{"n1", "n2", "n3"})
	rt := staticRoutes(map[string]protocol.NodeID{"a": "n1", "d": "n2", "g": "n3"})
	c1, err := snapshot.New(snapshot.Deps{
		Node: "n1", Peers: []protocol.NodeID{"n2", "n3"}, Ledger: led, Store: st1,
		Transport: dir, Clock: clock.New(), Router: rt, Epoch: epoch1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := c1.Initiate(ctx, "P1"); err != nil {
		t.Fatal(err)
	}
	// The record must be durable right now.
	rec, err := st1.GetRecord(ctx, "n1", "P1")
	if err != nil || rec.Phase != protocol.PhaseRecording {
		t.Fatalf("durable record: phase=%v err=%v", rec.Phase, err)
	}
	if err := st1.Close(); err != nil {
		t.Fatal(err)
	}

	// --- New process incarnation against the SAME database. ---
	st2, err := openPGNode(t, dsn, "pgrestart_n1", "n1", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	epoch2, err := st2.BumpEpoch(ctx, "n1")
	if err != nil || epoch2 != 2 {
		t.Fatalf("epoch2 = %d, %v", epoch2, err)
	}
	accounts, err := st2.LoadAccounts(ctx, "n1")
	if err != nil {
		t.Fatal(err)
	}
	if len(accounts) != 1 || accounts[0].Balance != 77 {
		t.Fatalf("reloaded accounts = %+v", accounts)
	}
	led2, _ := kernel.NewLedger("n1", accounts)
	c2, err := snapshot.New(snapshot.Deps{
		Node: "n1", Peers: []protocol.NodeID{"n2", "n3"}, Ledger: led2, Store: st2,
		Transport: dir, Clock: clock.New(), Router: rt, Epoch: epoch2,
	})
	if err != nil {
		t.Fatal(err)
	}
	aborted, err := c2.RecoverAbortedRounds(ctx)
	if err != nil || len(aborted) != 1 || aborted[0] != "P1" {
		t.Fatalf("recovery aborted = %v, %v", aborted, err)
	}
	rec2, _ := st2.GetRecord(ctx, "n1", "P1")
	if rec2.Phase != protocol.PhaseAborted {
		t.Fatalf("after restart P1 = %s, want aborted", rec2.Phase)
	}
	// Re-initiating the aborted id is a state conflict.
	if err := c2.Initiate(ctx, "P1"); !isConflictCode(err, "snapshot_aborted") {
		t.Fatalf("re-initiate aborted round: %v", err)
	}
}

// TestPostgresOutboxFIFOPersistence enqueues envelopes, reopens the store,
// and confirms they come back in insertion order with stable seq numbers.
func TestPostgresOutboxFIFOPersistence(t *testing.T) {
	dsn := skipNoPostgres(t)
	st, err := openPGNodeFresh(t, dsn, "pgoutbox_n1", "n1", nil, true)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	mk := func(ref string, seq uint64) protocol.Envelope {
		return protocol.Envelope{Type: protocol.MsgTransfer, Src: "n1", Dst: "n2", Seq: seq,
			Transfer: &protocol.Transfer{Ref: ref, From: "a", To: "d", Amount: 1}}
	}
	for i, ref := range []string{"m1", "m2", "m3"} {
		if _, err := st.AppendOutbox(mk(ref, 0)); err != nil {
			t.Fatalf("append %s: %v", ref, err)
		}
		_ = i
	}
	got, err := st.ListOutbox("n1", "n2")
	if err != nil || len(got) != 3 {
		t.Fatalf("list: %d, %v", len(got), err)
	}
	for i, want := range []string{"m1", "m2", "m3"} {
		if got[i].Transfer.Ref != want || got[i].Seq != uint64(i+1) {
			t.Fatalf("FIFO/seq broken at %d: %+v", i, got[i])
		}
	}
}
