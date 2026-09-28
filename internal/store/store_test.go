package store

import (
	"context"
	"database/sql"
	"errors"
	"net/netip"
	"testing"

	"rib/internal/netmodel"
)

func ptr(a netip.Addr) *netip.Addr { return &a }

func testStore(t *testing.T) *Store {
	t.Helper()
	// 每个测试独立内存库，避免共享缓存串数据。
	st, err := Open(context.Background(), "file:"+t.Name()+"?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func mkRoute(id, prefix, nh string) netmodel.Route {
	a, _ := netip.ParseAddr(nh)
	return netmodel.Route{
		ID:            id,
		Prefix:        netmodel.MustPrefix(prefix),
		AdminDistance: 5,
		Metric:        0,
		Protocol:      "static",
		Nexthop:       netmodel.Nexthop{Kind: netmodel.NHAddress, Address: ptr(a.Unmap())},
	}
}

func TestCommitAndLoadRoundTrip(t *testing.T) {
	ctx := context.Background()
	st := testStore(t)
	rt := mkRoute("r1", "10.0.0.0/8", "10.0.0.1")
	seq, err := st.CommitUpsert(ctx, rt, 2, "req-1")
	if err != nil {
		t.Fatal(err)
	}
	if seq != 1 {
		t.Fatalf("first seq=%d want 1", seq)
	}

	loaded, err := st.LoadRoutes(ctx)
	if err != nil || len(loaded) != 1 {
		t.Fatalf("load=%v n=%d err=%v", loaded, len(loaded), err)
	}
	got := loaded[0]
	if got.ID != "r1" || got.Prefix.String() != "10.0.0.0/8" {
		t.Fatalf("round trip mismatch: %+v", got)
	}
	if got.Nexthop.Address.String() != "10.0.0.1" {
		t.Fatalf("nexthop lost: %v", got.Nexthop.Address)
	}

	// 同键覆盖：快照仍是一行，事件多一条，版本更新。
	rt2 := rt
	rt2.Metric = 99
	if _, err := st.CommitUpsert(ctx, rt2, 3, "req-2"); err != nil {
		t.Fatal(err)
	}
	loaded, _ = st.LoadRoutes(ctx)
	if len(loaded) != 1 || loaded[0].Metric != 99 {
		t.Fatalf("upsert overwrite failed: %+v", loaded)
	}
	events, _ := st.EventsSince(ctx, 0, 0)
	if len(events) != 2 || events[1].RequestID != "req-2" {
		t.Fatalf("events=%+v", events)
	}
}

func TestDeleteMissingIsNoRowsAndNoEvent(t *testing.T) {
	ctx := context.Background()
	st := testStore(t)
	_, err := st.CommitDelete(ctx, 4, "10.0.0.0/8", "ghost", 1, "req-x")
	if !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("want sql.ErrNoRows, got %v", err)
	}
	events, _ := st.EventsSince(ctx, 0, 0)
	if len(events) != 0 {
		t.Fatalf("missing delete must not append event, got %d", len(events))
	}
}

func TestReplaceClearsSnapshotAndAppendsOneEvent(t *testing.T) {
	ctx := context.Background()
	st := testStore(t)
	_, _ = st.CommitUpsert(ctx, mkRoute("old", "10.0.0.0/8", "10.0.0.1"), 1, "a")

	req := ReplacePayload{
		V4: []netmodel.Route{mkRoute("new1", "192.0.2.0/24", "192.0.2.1")},
		V6: []netmodel.Route{{
			ID:            "v6x",
			Prefix:        netmodel.MustPrefix("2001:db8::/32"),
			AdminDistance: 5,
			Nexthop:       netmodel.Nexthop{Kind: netmodel.NHBlackhole},
		}},
	}
	seq, err := st.CommitReplace(ctx, req, 4, "bulk")
	if err != nil {
		t.Fatal(err)
	}
	loaded, _ := st.LoadRoutes(ctx)
	if len(loaded) != 2 {
		t.Fatalf("snapshot after replace has %d rows, want 2", len(loaded))
	}
	for _, rt := range loaded {
		if rt.ID == "old" {
			t.Fatal("old route survived replace snapshot")
		}
	}
	events, _ := st.EventsSince(ctx, 0, 0)
	// 1 upsert + 1 replace_all，replace 只产生一条事件。
	if len(events) != 2 || events[1].Seq != seq || events[1].Type != EventReplaceAll {
		t.Fatalf("events after replace=%+v", events)
	}
}

func TestEventsSinceAndMeta(t *testing.T) {
	ctx := context.Background()
	st := testStore(t)
	for i := 1; i <= 3; i++ {
		_, err := st.CommitUpsert(ctx,
			mkRoute("r", "10.0.0.0/8", "10.0.0.1"), int64(i), "rid")
		if err != nil {
			t.Fatal(err)
		}
	}
	got, _ := st.EventsSince(ctx, 1, 0)
	if len(got) != 2 || got[0].Seq != 2 {
		t.Fatalf("since=1 returned %+v", got)
	}
	limited, _ := st.EventsSince(ctx, 0, 1)
	if len(limited) != 1 {
		t.Fatalf("limit returned %d", len(limited))
	}
	if err := st.SetMeta(ctx, "k", "v"); err != nil {
		t.Fatal(err)
	}
	v, err := st.GetMeta(ctx, "k")
	if err != nil || v != "v" {
		t.Fatalf("meta=%q err=%v", v, err)
	}
	v, err = st.GetMeta(ctx, "missing")
	if err != nil || v != "" {
		t.Fatalf("missing meta should be empty-nil, got %q %v", v, err)
	}
}
