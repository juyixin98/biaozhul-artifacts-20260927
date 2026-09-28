package store_test

import (
	"context"
	"errors"
	"testing"

	"cidrsvc/internal/store"
)

func openMem(t *testing.T) *store.SQLiteStore {
	t.Helper()
	// A unique in-memory DSN per test via shared cache.
	dsn := "file:" + t.Name() + "?mode=memory&cache=shared"
	st, err := store.Open(context.Background(), dsn)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func sampleRec(id, status string) store.Record {
	return store.Record{
		RequestID: id, Family: "ipv4", Width: 32,
		AllowJSON: `["10.0.0.0/24"]`, ExclJSON: `["10.0.0.128/25"]`,
		Status:     status,
		ResultJSON: `["10.0.0.0/25"]`,
		StepsJSON:  `[]`,
	}
}

func TestSaveAndGetRoundTrip(t *testing.T) {
	st := openMem(t)
	ctx := context.Background()
	if err := st.Save(ctx, sampleRec("req_a", "ok")); err != nil {
		t.Fatal(err)
	}
	got, err := st.Get(ctx, "req_a")
	if err != nil {
		t.Fatal(err)
	}
	if got.Family != "ipv4" || got.Width != 32 || got.Status != "ok" ||
		got.ResultJSON != `["10.0.0.0/25"]` {
		t.Fatalf("round trip mismatch: %+v", got)
	}
	if got.CreatedAt.IsZero() {
		t.Fatal("created_at not populated")
	}
}

func TestGetMissingReturnsErrNotFound(t *testing.T) {
	st := openMem(t)
	_, err := st.Get(context.Background(), "nope")
	if !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestListFiltersAndOrdering(t *testing.T) {
	st := openMem(t)
	ctx := context.Background()
	recs := []store.Record{
		sampleRec("a", "ok"), sampleRec("b", "error"), sampleRec("c", "ok"),
	}
	recs[1].Family = "ipv6"
	recs[1].Width = 128
	for _, r := range recs {
		if err := st.Save(ctx, r); err != nil {
			t.Fatal(err)
		}
	}

	all, err := st.List(ctx, 50, 0, "", "")
	if err != nil || len(all) != 3 {
		t.Fatalf("all: n=%d err=%v", len(all), err)
	}

	errs, err := st.List(ctx, 50, 0, "", "error")
	if err != nil || len(errs) != 1 || errs[0].RequestID != "b" {
		t.Fatalf("status filter: %+v err=%v", errs, err)
	}

	v6, err := st.List(ctx, 50, 0, "ipv6", "")
	if err != nil || len(v6) != 1 || v6[0].RequestID != "b" {
		t.Fatalf("family filter: %+v err=%v", v6, err)
	}

	ok, err := st.List(ctx, 50, 0, "ipv4", "ok")
	if err != nil || len(ok) != 2 {
		t.Fatalf("combined filter: n=%d err=%v", len(ok), err)
	}
}

func TestCountByStatus(t *testing.T) {
	st := openMem(t)
	ctx := context.Background()
	if err := st.Save(ctx, sampleRec("a", "ok")); err != nil {
		t.Fatal(err)
	}
	if err := st.Save(ctx, sampleRec("b", "ok")); err != nil {
		t.Fatal(err)
	}
	if err := st.Save(ctx, sampleRec("c", "error")); err != nil {
		t.Fatal(err)
	}
	counts, err := st.CountByStatus(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if counts["ok"] != 2 || counts["error"] != 1 {
		t.Fatalf("counts=%v", counts)
	}
}

func TestReopenPersists(t *testing.T) {
	dir := t.TempDir() + "/test.db"
	ctx := context.Background()
	st1, err := store.Open(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := st1.Save(ctx, sampleRec("persist_1", "ok")); err != nil {
		t.Fatal(err)
	}
	if err := st1.Close(); err != nil {
		t.Fatal(err)
	}
	st2, err := store.Open(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	got, err := st2.Get(ctx, "persist_1")
	if err != nil || got.Status != "ok" {
		t.Fatalf("after reopen: %+v err=%v", got, err)
	}
}
