package ribd_test

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/opp221/ribd/internal/netmodel"
	"github.com/opp221/ribd/internal/replay"
	"github.com/opp221/ribd/internal/rib"
	"github.com/opp221/ribd/internal/store"
)

func TestPersistReplayAndVerify(t *testing.T) {
	ctx := context.Background()
	dsn := filepath.Join(t.TempDir(), "rib.db")
	st, err := store.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	tbl := rib.NewTable(3, st)
	r1 := netmodel.Route{ID: "d", Prefix: netmodel.MustPrefix("0.0.0.0/0"), AdminDist: 10,
		NextHop: netmodel.NextHop{Interface: "eth0"}}
	r2 := netmodel.Route{ID: "p", Prefix: netmodel.MustPrefix("203.0.113.0/24"),
		NextHop: netmodel.NextHop{Interface: "eth1"}}

	s1, err := tbl.Apply([]rib.Change{{Kind: rib.Upsert, Route: r1}})
	if err != nil {
		t.Fatal(err)
	}
	if s1.VersionNum() != 1 {
		t.Fatalf("first batch version = %d", s1.VersionNum())
	}
	if _, err := tbl.Apply([]rib.Change{{Kind: rib.Upsert, Route: r2}}); err != nil {
		t.Fatal(err)
	}
	if _, err := tbl.Apply([]rib.Change{{Kind: rib.Delete, Route: netmodel.Route{ID: "d"}}}); err != nil {
		t.Fatal(err)
	}

	head, err := st.HeadVersion(ctx)
	if err != nil || head != 3 {
		t.Fatalf("head = %d, err=%v", head, err)
	}

	// Reconstruct from version 1 only.
	res1, err := replay.FromStore(ctx, st, 3, 1)
	if err != nil {
		t.Fatal(err)
	}
	if res1.Version != 1 || len(res1.Snap.Routes()) != 1 {
		t.Fatalf("replay to v1: version=%d routes=%d", res1.Version, len(res1.Snap.Routes()))
	}
	if r := res1.Snap.LookupText("198.51.100.1"); r.Egress != "eth0" {
		t.Fatalf("replayed v1 lookup wrong: %+v", r)
	}

	// Full reconstruction matches live state.
	res3, err := replay.FromStore(ctx, st, 3, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(res3.Snap.Routes()) != 1 {
		t.Fatalf("after delete, want 1 route, got %d", len(res3.Snap.Routes()))
	}

	// Snapshot verification agrees with reconstruction at every version.
	for _, v := range []uint64{1, 2, 3} {
		mm, err := replay.VerifyAt(ctx, st, 3, v)
		if err != nil {
			t.Fatalf("verify v%d: %v", v, err)
		}
		if len(mm) != 0 {
			t.Fatalf("verify v%d reported mismatches: %+v", v, mm)
		}
	}
}

func TestPersistedBatchAtomicity(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(ctx, filepath.Join(t.TempDir(), "rib.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	tbl := rib.NewTable(3, st)
	// First a valid batch.
	ok := netmodel.Route{ID: "ok", Prefix: netmodel.MustPrefix("10.0.0.0/8"),
		NextHop: netmodel.NextHop{Interface: "eth0"}}
	if _, err := tbl.Apply([]rib.Change{{Kind: rib.Upsert, Route: ok}}); err != nil {
		t.Fatal(err)
	}
	// Second batch invalid: persistence never reached, head stays at 1.
	bad := netmodel.Route{ID: "", Prefix: netmodel.MustPrefix("10.0.0.0/8"),
		NextHop: netmodel.NextHop{Interface: "eth0"}}
	if _, err := tbl.Apply([]rib.Change{{Kind: rib.Upsert, Route: bad}}); err == nil {
		t.Fatal("invalid batch accepted")
	}
	head, _ := st.HeadVersion(ctx)
	if head != 1 {
		t.Fatalf("head moved to %d after rejected batch", head)
	}
	evs, err := st.Events(ctx, 0)
	if err != nil || len(evs) != 1 {
		t.Fatalf("events = %d, err=%v (rejected batch must not leave an event)", len(evs), err)
	}
}

func TestRestartAdoptReplaysState(t *testing.T) {
	ctx := context.Background()
	dsn := filepath.Join(t.TempDir(), "rib.db")
	st, err := store.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	tbl := rib.NewTable(3, st)
	r := netmodel.Route{ID: "h", Prefix: netmodel.MustPrefix("198.51.100.7/32"),
		NextHop: netmodel.NextHop{Interface: "eth9"}}
	if _, err := tbl.Apply([]rib.Change{{Kind: rib.Upsert, Route: r}}); err != nil {
		t.Fatal(err)
	}
	st.Close()

	// Reopen, replay, adopt into a fresh table.
	st2, err := store.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	head, _ := st2.HeadVersion(ctx)
	res, err := replay.FromStore(ctx, st2, 3, head)
	if err != nil {
		t.Fatal(err)
	}
	tbl2 := rib.NewTable(3, nil)
	if err := tbl2.Adopt(res.Snap); err != nil {
		t.Fatal(err)
	}
	got := tbl2.Current().LookupText("198.51.100.7")
	if got.Egress != "eth9" {
		t.Fatalf("post-restart lookup: %+v", got)
	}
}
