package store_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"cidrcov/internal/store"
)

func TestSaveGetRoundTrip(t *testing.T) {
	ctx := context.Background()
	s, err := store.Open(ctx, ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	now := time.Now().UTC().Add(time.Second) // future so it is unambiguously non-zero
	rec := store.Record{
		RequestID:    "req-abc",
		CreatedAt:    now,
		AllowInput:   `["10.0.0.0/24"]`,
		ExcludeInput: `["10.0.0.5/32"]`,
		Status:       "ok",
		ResultJSON:   `{"status":"ok"}`,
	}
	if err := s.Save(ctx, rec); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get(ctx, "req-abc")
	if err != nil {
		t.Fatal(err)
	}
	if got.AllowInput != rec.AllowInput || got.Status != "ok" || got.ResultJSON == "" {
		t.Fatalf("round trip mismatch: %+v", got)
	}
	if got.CreatedAt.IsZero() {
		t.Fatalf("created_at should be parsed")
	}
	if !got.CreatedAt.Equal(now) {
		t.Fatalf("created_at round trip: got %s want %s", got.CreatedAt, now)
	}
}

func TestGetNotFound(t *testing.T) {
	s, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if _, err := s.Get(context.Background(), "missing"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestDuplicateIDRejected(t *testing.T) {
	s, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	rec := store.Record{RequestID: "dup", AllowInput: "[]", ExcludeInput: "[]",
		Status: "empty", ResultJSON: "{}"}
	if err := s.Save(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	if err := s.Save(context.Background(), rec); !errors.Is(err, store.ErrDuplicate) {
		t.Fatalf("want ErrDuplicate, got %v", err)
	}
}

func TestRecentOrdering(t *testing.T) {
	s, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	for _, id := range []string{"a", "b", "c"} {
		if err := s.Save(context.Background(), store.Record{
			RequestID: id, AllowInput: "[]", ExcludeInput: "[]",
			Status: "empty", ResultJSON: "{}"}); err != nil {
			t.Fatal(err)
		}
	}
	recs, err := s.Recent(context.Background(), 10)
	if err != nil {
		t.Fatal(err)
	}
	// Same-timestamp ties break on request_id DESC, so c leads.
	if len(recs) != 3 || recs[0].RequestID != "c" {
		t.Fatalf("unexpected recent order: %+v", recs)
	}
}
