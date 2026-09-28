package storage

import (
	"context"
	"errors"
	"testing"

	"admission/internal/model"
	"admission/internal/plugins"
)

func openStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestSQLQuota_HoldThenCommitIsExact(t *testing.T) {
	s := openStore(t)
	q := NewSQLQuota(s, map[string]int64{"Widget": 5})
	ctx := context.Background()

	// Validation-time hold.
	if err := q.Reserve(ctx, "u-a", "Widget", 3, model.OpCreate); err != nil {
		t.Fatalf("reserve 3: %v", err)
	}
	// Same UID again must be a no-op even though it would exceed the limit.
	if err := q.Reserve(ctx, "u-a", "Widget", 3, model.OpCreate); err != nil {
		t.Fatalf("idempotent re-reserve: %v", err)
	}
	// A different UID over the remaining headroom is rejected (3 hold + 3 > 5).
	err := q.Reserve(ctx, "u-b", "Widget", 3, model.OpCreate)
	if !errors.Is(err, plugins.ErrQuotaExhausted) {
		t.Fatalf("want ErrQuotaExhausted, got %v", err)
	}
	// Within remaining headroom succeeds.
	if err := q.Reserve(ctx, "u-c", "Widget", 2, model.OpCreate); err != nil {
		t.Fatalf("reserve 2 within limit: %v", err)
	}
	// Before commit, holds do not count as committed usage.
	if used, _ := q.Used(ctx, "Widget"); used != 0 {
		t.Fatalf("uncommitted holds must not count as used, got %d", used)
	}
	// Commit u-a (3 replicas): hold consumed, committed usage is now 3.
	req := model.Request{UID: "u-a", Operation: model.OpCreate, Object: widget("w-a", 3)}
	if err := s.CommitOutcome(ctx, "run-a", req, allowResp(req), 1); err != nil {
		t.Fatalf("commit u-a: %v", err)
	}
	used, _ := q.Used(ctx, "Widget")
	if used != 3 {
		t.Fatalf("committed used = %d, want 3", used)
	}
	// u-c's hold (2) plus committed 3 == 5 fits; commit it.
	reqC := model.Request{UID: "u-c", Operation: model.OpCreate, Object: widget("w-c", 2)}
	if err := s.CommitOutcome(ctx, "run-c", reqC, allowResp(reqC), 2); err != nil {
		t.Fatalf("commit u-c: %v", err)
	}
	if used, _ := q.Used(ctx, "Widget"); used != 5 {
		t.Fatalf("used = %d, want 5", used)
	}
}

func TestSQLQuota_UpdateAndDeleteKeepAccountingExact(t *testing.T) {
	s := openStore(t)
	q := NewSQLQuota(s, map[string]int64{"Widget": 5})
	ctx := context.Background()

	// Create with 3.
	reqC := model.Request{UID: "u1", Operation: model.OpCreate, Object: widget("w1", 3)}
	if err := s.CommitOutcome(ctx, "run-1", reqC, allowResp(reqC), 1); err != nil {
		t.Fatal(err)
	}
	if used, _ := q.Used(ctx, "Widget"); used != 3 {
		t.Fatalf("after create used=%d want 3", used)
	}

	// Update to 5: only the +2 delta is held at validation.
	if err := q.Reserve(ctx, "u2", "Widget", 2, model.OpUpdate); err != nil {
		t.Fatalf("update hold: %v", err)
	}
	obj5 := widget("w1", 5)
	reqU := model.Request{UID: "u2", Operation: model.OpUpdate, Object: obj5,
		OldObject: widget("w1", 3)}
	if err := s.CommitOutcome(ctx, "run-2", reqU, allowResp(reqU), 2); err != nil {
		t.Fatalf("commit update: %v", err)
	}
	if used, _ := q.Used(ctx, "Widget"); used != 5 {
		t.Fatalf("after update used=%d want 5 (no double counting of old create)", used)
	}

	// Delete frees all capacity.
	reqD := model.Request{UID: "u3", Operation: model.OpDelete, OldObject: obj5}
	if err := s.CommitOutcome(ctx, "run-3", reqD, allowResp(reqD), 3); err != nil {
		t.Fatalf("commit delete: %v", err)
	}
	if used, _ := q.Used(ctx, "Widget"); used != 0 {
		t.Fatalf("after delete used=%d want 0", used)
	}
	// And the name can be recreated at full limit again.
	reqR := model.Request{UID: "u4", Operation: model.OpCreate, Object: widget("w1", 5)}
	if err := s.CommitOutcome(ctx, "run-4", reqR, allowResp(reqR), 4); err != nil {
		t.Fatalf("recreate after delete: %v", err)
	}
}

func TestSQLQuota_CommitGateRejectsOverLimit(t *testing.T) {
	// Even if a resource is committed without a prior hold, the in-tx gate
	// enforces the limit against real committed usage.
	s := openStore(t)
	_ = NewSQLQuota(s, map[string]int64{"Widget": 2})
	ctx := context.Background()
	req1 := model.Request{UID: "g1", Operation: model.OpCreate, Object: widget("wg", 2)}
	if err := s.CommitOutcome(ctx, "r1", req1, allowResp(req1), 1); err != nil {
		t.Fatal(err)
	}
	req2 := model.Request{UID: "g2", Operation: model.OpCreate, Object: widget("wg2", 1)}
	err := s.CommitOutcome(ctx, "r2", req2, allowResp(req2), 2)
	if !errors.Is(err, plugins.ErrQuotaExhausted) {
		t.Fatalf("commit gate must reject over-limit, got %v", err)
	}
	// Rejected transaction left no resource behind and usage is unchanged.
	if _, err := s.GetResource(ctx, "Widget", "ns", "wg2"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("over-limit resource must not be committed, got err=%v", err)
	}
	if used, _ := NewSQLQuota(s, map[string]int64{"Widget": 2}).Used(ctx, "Widget"); used != 2 {
		t.Fatalf("used=%d want 2 after rejected commit", used)
	}
}

func TestCommitOutcome_AllowsThenDeniedReleasesHold(t *testing.T) {
	s := openStore(t)
	q := NewSQLQuota(s, map[string]int64{"Widget": 2})
	ctx := context.Background()

	// Simulate a successful validation reserve + commit.
	req := model.Request{UID: "c1", Operation: model.OpCreate, Object: widget("w1", 1)}
	if err := q.Reserve(ctx, "c1", "Widget", 1, model.OpCreate); err != nil {
		t.Fatal(err)
	}
	resp := allowResp(req)
	if err := s.CommitOutcome(ctx, "run-1", req, resp, 1); err != nil {
		t.Fatalf("commit allowed: %v", err)
	}
	used, _ := q.Used(ctx, "Widget")
	if used != 1 {
		t.Fatalf("committed used = %d, want 1", used)
	}

	// A denied request that had reserved must release its hold.
	req2 := model.Request{UID: "c2", Operation: model.OpCreate, Object: widget("w2", 1)}
	if err := q.Reserve(ctx, "c2", "Widget", 1, model.OpCreate); err != nil {
		t.Fatal(err)
	}
	denied := model.Response{UID: "c2", Decision: model.DecisionDenied,
		Reason: model.ReasonValidationDenied, Message: "nope", Steps: []model.Step{}}
	if err := s.CommitOutcome(ctx, "run-2", req2, denied, 2); err != nil {
		t.Fatalf("commit denied: %v", err)
	}
	// c3 can now reserve the freed slot (total committed+held stays 2).
	if err := q.Reserve(ctx, "c3", "Widget", 1, model.OpCreate); err != nil {
		t.Fatalf("expected freed hold to be reservable: %v", err)
	}
}

func TestCommitOutcome_CreateNameConflict(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()
	req1 := model.Request{UID: "k1", Operation: model.OpCreate, Object: widget("dup", 1)}
	if err := s.CommitOutcome(ctx, "run-1", req1, allowResp(req1), 1); err != nil {
		t.Fatal(err)
	}
	req2 := model.Request{UID: "k2", Operation: model.OpCreate, Object: widget("dup", 1)}
	err := s.CommitOutcome(ctx, "run-2", req2, allowResp(req2), 2)
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("want ErrConflict on duplicate name, got %v", err)
	}
}

func TestClaim_ReclaimsStaleLease(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()
	const now100 int64 = 100
	insert := func(uid string) {
		existed, err := s.InsertRequest(ctx, `{"uid":"`+uid+`"}`, uid, model.OpCreate, 1)
		if err != nil || existed {
			t.Fatalf("insert %s: existed=%v err=%v", uid, existed, err)
		}
	}
	insert("p1")
	uid, payload, ok, err := s.Claim(ctx, now100, now100+30_000)
	if err != nil || !ok || uid != "p1" {
		t.Fatalf("first claim: uid=%s ok=%v err=%v", uid, ok, err)
	}
	if payload == "" {
		t.Fatal("payload empty")
	}
	// While lease is fresh, nothing is claimable.
	if _, _, ok, _ := s.Claim(ctx, now100+1000, now100+31_000); ok {
		t.Fatal("fresh processing lease must not be reclaimable")
	}
	// After lease expiry it is.
	if _, _, ok, _ = s.Claim(ctx, now100+40_000, now100+70_000); !ok {
		t.Fatal("stale processing lease must be reclaimable")
	}
}

func widget(name string, replicas int64) map[string]any {
	return map[string]any{
		"apiVersion": "v1", "kind": "Widget",
		"metadata": map[string]any{"name": name, "namespace": "ns"},
		"spec":     map[string]any{"replicas": replicas, "capacity": replicas * 100},
	}
}

func allowResp(req model.Request) model.Response {
	obj := req.Object
	if len(obj) == 0 {
		obj = req.OldObject // DELETE carries identity in oldObject
	}
	sum, _ := model.Summarize(obj)
	return model.Response{
		UID: req.UID, Decision: model.DecisionAllowed,
		FinalObject: obj, FinalSummary: &sum, Steps: []model.Step{},
	}
}
