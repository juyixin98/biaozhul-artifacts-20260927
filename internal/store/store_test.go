package store

import (
	"context"
	"testing"

	"pathvector/internal/ierr"
)

func TestSaveGetListTrace(t *testing.T) {
	ctx := context.Background()
	s, err := Open(ctx, ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	report := map[string]any{
		"run_id": "run-A", "status": "converged", "reason": "quiesced", "steps": 3,
		"trace": []map[string]any{
			{"step": 1, "router": "a", "outcome": "accepted"},
			{"step": 2, "router": "b", "outcome": "rejected_loop"},
		},
	}
	if err := s.SaveRun(ctx, SaveInput{
		RunID: "run-A", Status: "converged", Reason: "quiesced", Steps: 3, Budget: 50,
		ConfigJSON: []byte(`{"topology":{}}`), Report: report,
	}); err != nil {
		t.Fatal(err)
	}

	rec, err := s.GetRun(ctx, "run-A")
	if err != nil {
		t.Fatal(err)
	}
	if rec.Status != "converged" || rec.Steps != 3 {
		t.Fatalf("record = %+v", rec)
	}
	if rec.ErrorKind != "" {
		t.Fatalf("clean run error_kind=%q", rec.ErrorKind)
	}

	trace, err := s.GetTrace(ctx, "run-A")
	if err != nil {
		t.Fatal(err)
	}
	if len(trace) != 2 {
		t.Fatalf("trace rows=%d want 2", len(trace))
	}

	rows, err := s.ListRuns(ctx, 10)
	if err != nil || len(rows) != 1 || rows[0].RunID != "run-A" {
		t.Fatalf("list rows=%+v err=%v", rows, err)
	}

	// Upsert semantics: same run id replaced, trace replaced.
	report2 := map[string]any{
		"run_id": "run-A", "status": "not_converged", "reason": "state_conflict", "steps": 4,
		"trace": []map[string]any{{"step": 1, "router": "a"}},
	}
	if err := s.SaveRun(ctx, SaveInput{
		RunID: "run-A", Status: "not_converged", Reason: "state_conflict",
		Steps: 4, Budget: 50, ErrorKind: "state_conflict", ErrorDetail: "boom",
		ConfigJSON: []byte(`{}`), Report: report2,
	}); err != nil {
		t.Fatal(err)
	}
	trace, _ = s.GetTrace(ctx, "run-A")
	if len(trace) != 1 {
		t.Fatalf("trace after upsert rows=%d want 1", len(trace))
	}
	rec, _ = s.GetRun(ctx, "run-A")
	if rec.ErrorKind != "state_conflict" {
		t.Fatalf("error_kind=%q", rec.ErrorKind)
	}
}

func TestNotFoundClassified(t *testing.T) {
	ctx := context.Background()
	s, err := Open(ctx, ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	if _, err := s.GetRun(ctx, "missing"); !ierr.Is(err, ierr.KindNotFound) {
		t.Fatalf("GetRun kind=%s want not_found", ierr.Of(err))
	}
	if _, err := s.GetTrace(ctx, "missing"); !ierr.Is(err, ierr.KindNotFound) {
		t.Fatalf("GetTrace kind=%s want not_found", ierr.Of(err))
	}
}
