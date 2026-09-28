package provider

import (
	"context"
	"testing"

	"infraplanner/internal/model"
)

func vpcD(name, cidr string) model.Desired {
	return model.Desired{Kind: model.KindVPC, Name: name,
		Attrs: map[string]string{"cidr": cidr, "region": "east"}}
}

func resolveNone(model.Ref) (string, error) { return "", nil }

func TestCreate_IdempotentByLogicalName(t *testing.T) {
	s := NewSim()
	ctx := context.Background()
	d := vpcD("main", "10/8")

	r1, err := s.Create(ctx, d, resolveNone)
	if err != nil {
		t.Fatalf("first create: %v", err)
	}
	// Second create after a "lost response" must NOT provision a second
	// physical resource; it returns the same id.
	r2, err := s.Create(ctx, d, resolveNone)
	if err != nil {
		t.Fatalf("second create: %v", err)
	}
	if r1.ID != r2.ID {
		t.Fatalf("ids differ: %s vs %s (duplicate create)", r1.ID, r2.ID)
	}
	if n := len(s.CountByKind()); n != 1 {
		t.Fatalf("kind count = %d, want 1", n)
	}
	if got := s.CountByKind()[model.KindVPC]; got != 1 {
		t.Fatalf("vpc count = %d, want 1", got)
	}
}

func TestCreate_CommitResponseLost(t *testing.T) {
	s := NewSim()
	ctx := context.Background()
	d := vpcD("main", "10/8")
	s.ArmFault(Fault{Kind: FaultCreateCommitLost, Target: d.Key()})

	_, err := s.Create(ctx, d, resolveNone)
	me, ok := model.AsError(err)
	if !ok || me.Category != model.CatCompute || me.Code != "response_lost" {
		t.Fatalf("want compute/response_lost, got %v", err)
	}
	// But the resource really committed.
	obs, _ := s.Observe(ctx)
	if len(obs.Resources) != 1 {
		t.Fatalf("after lost response, live count = %d, want 1 (committed)", len(obs.Resources))
	}
	// Retrying create adopts the committed resource, no duplicate.
	r, err := s.Create(ctx, d, resolveNone)
	if err != nil {
		t.Fatalf("retry create: %v", err)
	}
	if r.ID != obs.Resources[0].ID {
		t.Fatal("retry created a new id instead of adopting committed resource")
	}
	if obs2, _ := s.Observe(ctx); len(obs2.Resources) != 1 {
		t.Fatalf("live count after retry = %d, want 1", len(obs2.Resources))
	}
}

func TestCreate_CapacityExhaustion(t *testing.T) {
	s := NewSim()
	s.SetCapacity(model.KindVPC, 1)
	ctx := context.Background()
	if _, err := s.Create(ctx, vpcD("a", "10/8"), resolveNone); err != nil {
		t.Fatal(err)
	}
	_, err := s.Create(ctx, vpcD("b", "10/8"), resolveNone)
	me, ok := model.AsError(err)
	if !ok || me.Category != model.CatExhaustion || me.Code != "capacity_full" {
		t.Fatalf("want resource_exhaustion/capacity_full, got %v", err)
	}
}

func TestCreate_InjectedExhaustion(t *testing.T) {
	s := NewSim()
	ctx := context.Background()
	d := vpcD("a", "10/8")
	s.ArmFault(Fault{Kind: FaultCreateExhaust, Target: d.Key()})
	_, err := s.Create(ctx, d, resolveNone)
	me, ok := model.AsError(err)
	if !ok || me.Category != model.CatExhaustion {
		t.Fatalf("want resource_exhaustion, got %v", err)
	}
	if n := s.CountByKind()[model.KindVPC]; n != 0 {
		t.Fatalf("failed create must not leave state, vpc count=%d", n)
	}
}

func TestUpdate_TransientThenSuccess(t *testing.T) {
	s := NewSim()
	ctx := context.Background()
	d := model.Desired{Kind: model.KindInstance, Name: "api",
		Attrs: map[string]string{"image": "img", "shape": "small"},
		Refs:  map[string]model.Ref{"subnet_ref": {Kind: model.KindSubnet, Name: "web"}}}
	sub := model.Desired{Kind: model.KindSubnet, Name: "web",
		Attrs: map[string]string{"cidr": "10/16"},
		Refs:  map[string]model.Ref{"network_ref": {Kind: model.KindVPC, Name: "main"}}}
	s.Seed(model.Live{Key: sub.Key(), ID: "id-sub"})
	resolve := func(r model.Ref) (string, error) { return "id-sub", nil }

	cr, err := s.Create(ctx, d, resolve)
	if err != nil {
		t.Fatal(err)
	}
	d.Attrs["shape"] = "large"
	s.ArmFault(Fault{Kind: FaultUpdateTransient, Target: d.Key()})
	if _, err := s.Update(ctx, cr.ID, d, resolve); err == nil {
		t.Fatal("want injected transient error")
	}
	// retry succeeds and keeps the same physical id
	live, err := s.Update(ctx, cr.ID, d, resolve)
	if err != nil {
		t.Fatalf("retry update: %v", err)
	}
	if live.ID != cr.ID {
		t.Fatalf("update changed id %s -> %s", cr.ID, live.ID)
	}
	if live.Attrs["shape"] != "large" {
		t.Fatalf("shape = %q, want large", live.Attrs["shape"])
	}
}

func TestUpdate_ImmutableFieldRejectedByAdapter(t *testing.T) {
	s := NewSim()
	ctx := context.Background()
	d := vpcD("main", "10/8")
	cr, _ := s.Create(ctx, d, resolveNone)
	d2 := vpcD("main", "172.16/12")
	_, err := s.Update(ctx, cr.ID, d2, resolveNone)
	me, ok := model.AsError(err)
	if !ok || me.Category != model.CatConflict || me.Code != "immutable_change" {
		t.Fatalf("want state_conflict/immutable_change, got %v", err)
	}
}

func TestDelete_IdempotentAndStaleIDSafe(t *testing.T) {
	s := NewSim()
	ctx := context.Background()
	k := model.Key{Kind: model.KindVPC, Name: "main"}
	// deleting what is already gone is success
	if err := s.Delete(ctx, "ghost-id", k); err != nil {
		t.Fatalf("delete absent = %v, want nil (idempotent)", err)
	}

	cr, _ := s.Create(ctx, vpcD("main", "10/8"), resolveNone)
	// Model an external delete-then-recreate: the old physical resource is
	// gone from byID, but a NEW resource now owns the same logical name. A
	// stale journaled delete carrying the old id must not remove the new one.
	s.mem.mu.Lock()
	delete(s.mem.byID, cr.ID)
	s.mem.byID["new-id"] = &model.Live{Key: k, ID: "new-id",
		Attrs: map[string]string{"cidr": "10/8", "region": "east"}}
	s.mem.nameIndex[k] = "new-id"
	s.mem.mu.Unlock()

	err := s.Delete(ctx, cr.ID, k)
	me, ok := model.AsError(err)
	if !ok || me.Category != model.CatConflict || me.Code != "id_reassigned" {
		t.Fatalf("want state_conflict/id_reassigned, got %v", err)
	}
	obs, _ := s.Observe(ctx)
	if len(obs.Resources) != 1 || obs.Resources[0].ID != "new-id" {
		t.Fatalf("stale delete harmed new resource: %+v", obs.Resources)
	}
}

func TestCreate_AmbiguousUnknownRollsBack(t *testing.T) {
	s := NewSim()
	ctx := context.Background()
	d := vpcD("a", "10/8")
	s.ArmFault(Fault{Kind: FaultCreateUnknown, Target: d.Key()})
	_, err := s.Create(ctx, d, resolveNone)
	me, ok := model.AsError(err)
	if !ok || me.Code != "outcome_unknown" {
		t.Fatalf("want compute/outcome_unknown, got %v", err)
	}
	// The ambiguous branch rolls the commit back: nothing is observable, so a
	// later retry cannot duplicate.
	if obs, _ := s.Observe(ctx); len(obs.Resources) != 0 {
		t.Fatalf("outcome_unknown must roll back, found %+v", obs.Resources)
	}
	// Retry commits exactly one resource.
	r, err := s.Create(ctx, d, resolveNone)
	if err != nil {
		t.Fatalf("retry: %v", err)
	}
	if obs, _ := s.Observe(ctx); len(obs.Resources) != 1 || obs.Resources[0].ID != r.ID {
		t.Fatalf("after ambiguous retry, want exactly 1, got %+v", obs.Resources)
	}
}

func TestDelete_TransientThenSuccess(t *testing.T) {
	s := NewSim()
	ctx := context.Background()
	d := vpcD("main", "10/8")
	cr, _ := s.Create(ctx, d, resolveNone)
	s.ArmFault(Fault{Kind: FaultDeleteTransient, Target: d.Key()})
	if err := s.Delete(ctx, cr.ID, d.Key()); err == nil {
		t.Fatal("want transient error")
	}
	if err := s.Delete(ctx, cr.ID, d.Key()); err != nil {
		t.Fatalf("retry delete: %v", err)
	}
	if obs, _ := s.Observe(ctx); len(obs.Resources) != 0 {
		t.Fatalf("after delete live count = %d, want 0", len(obs.Resources))
	}
}
