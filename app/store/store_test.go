package store

import (
	"path/filepath"
	"testing"

	"replicactl/core/controller"
	"replicactl/core/model"
)

func openTestDB(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	dsn := "file:" + filepath.Join(dir, "test.db")
	db, err := Open(dsn)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return dsn
}

func TestDurableFixtureSurvivesRestart(t *testing.T) {
	cfg := model.DefaultConfig()
	dsn := openTestDB(t)

	// First "process": fleet scales 0 -> 1 -> 4, one metric lands, a demand posts.
	db1, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	fx1, err := NewDurableFixture(db1, cfg)
	if err != nil {
		t.Fatal(err)
	}
	fx1.Clock = func() int64 { return 1000 }
	if err := fx1.SetReplicas(4); err != nil {
		t.Fatal(err)
	}
	if err := fx1.SubmitSample(model.LoadSample{InstanceID: "instance-001", Load: 17, ReportedAt: 1000}); err != nil {
		t.Fatal(err)
	}
	if err := fx1.PostDemand(true, 1000); err != nil {
		t.Fatal(err)
	}
	decisions := NewDecisionLog(db1)
	history := NewRawPointLog(db1)
	if err := history.AppendPoint(controller.RawPoint{At: 1000, RawDesired: 4}); err != nil {
		t.Fatal(err)
	}
	if _, err := decisions.AppendDecision(controller.Decision{
		RequestID: "restart-1", TickAt: 1000, Action: controller.ActionScaleUp,
		CurrentReplicas: 1, DesiredReplicas: 4,
	}); err != nil {
		t.Fatal(err)
	}
	db1.Close()

	// Second "process" against the same file: every piece of state restores.
	db2, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	fx2, err := NewDurableFixture(db2, cfg)
	if err != nil {
		t.Fatal(err)
	}
	n, err := fx2.CurrentReplicas()
	if err != nil || n != 4 {
		t.Fatalf("after restart replicas=%d err=%v, want 4", n, err)
	}
	samples, err := fx2.LatestSamples(1000)
	if err != nil || len(samples) != 4 {
		t.Fatalf("samples after restart: %v %v", samples, err)
	}
	if samples[0].Missing || samples[0].Load != 17 {
		t.Fatalf("instance-001 report lost across restart: %+v", samples[0])
	}
	if !samples[1].Missing {
		t.Fatalf("instance-002 should be missing: %+v", samples[1])
	}
	demand, ok, err := fx2.LatestDemand(1000)
	if err != nil || !ok || !demand.Present || demand.ReportedAt != 1000 {
		t.Fatalf("demand after restart: %+v ok=%v err=%v", demand, ok, err)
	}
	pts, err := NewRawPointLog(db2).RawPointsSince(900, 1100)
	if err != nil || len(pts) != 1 || pts[0].RawDesired != 4 {
		t.Fatalf("raw history after restart: %+v err=%v", pts, err)
	}
	d, found, err := NewDecisionLog(db2).ByRequestID("restart-1")
	if err != nil || !found || d.DesiredReplicas != 4 {
		t.Fatalf("decision after restart found=%v err=%v d=%+v", found, err, d)
	}
}

func TestRestartWithZeroReplicasUsesZeroPolicy(t *testing.T) {
	// A service restarted while the fleet was scaled to zero must not invent
	// metrics: reconcile reads zero instances and waits for a demand signal.
	cfg := model.DefaultConfig()
	dsn := openTestDB(t)
	db, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	fx, err := NewDurableFixture(db, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := fx.SetReplicas(0); err != nil {
		t.Fatal(err)
	}
	decisions := NewDecisionLog(db)
	history := NewRawPointLog(db)
	ctl, err := controller.New(cfg, fx, fx, decisions, history)
	if err != nil {
		t.Fatal(err)
	}
	d, err := ctl.Reconcile(5000, "post-restart-zero")
	if err != nil {
		t.Fatal(err)
	}
	if d.Action != controller.ActionNoop {
		t.Fatalf("got %s want noop", d.Action)
	}
	found := false
	for _, r := range d.Reasons {
		if r == controller.ReasonNoopZeroNoDemand {
			found = true
		}
	}
	if !found {
		t.Fatalf("reasons %v want ZERO_NO_DEMAND", d.Reasons)
	}
}

func TestDecisionObservationRoundTrip(t *testing.T) {
	db, err := Open(openTestDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	log := NewDecisionLog(db)
	in := controller.Decision{
		RequestID: "obs-1", TickAt: 9000, Action: controller.ActionScaleUp,
		CurrentReplicas: 2, DesiredReplicas: 4,
		Reasons: []controller.Reason{controller.ReasonScaleUpRateLimited},
		Observation: &controller.Observation{
			At: 9000, CurrentReplicas: 2, FreshCount: 2, TotalLoad: 60,
			RawDesired: 6, ClampedDesired: 6, UtilisationRatio: 3,
			TargetPerInstance: 10, FreshFraction: 1,
			Samples: []controller.SampleView{{InstanceID: "instance-001", Status: "fresh", Load: 30, UsedLoad: 30}},
		},
	}
	saved, err := log.AppendDecision(in)
	if err != nil {
		t.Fatal(err)
	}
	if saved.ID == 0 {
		t.Fatal("decision id not assigned")
	}
	got, ok, err := log.ByRequestID("obs-1")
	if err != nil || !ok {
		t.Fatalf("lookup: ok=%v err=%v", ok, err)
	}
	if got.Observation == nil || got.Observation.TotalLoad != 60 || got.Observation.RawDesired != 6 {
		t.Fatalf("observation not persisted: %+v", got.Observation)
	}
	if len(got.Observation.Samples) != 1 || got.Observation.Samples[0].InstanceID != "instance-001" {
		t.Fatalf("sample views lost: %+v", got.Observation.Samples)
	}
}

func TestRawPointUpsertKeepsOneRowPerTick(t *testing.T) {
	db, err := Open(openTestDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	h := NewRawPointLog(db)
	for i := 0; i < 3; i++ {
		if err := h.AppendPoint(controller.RawPoint{At: 7, RawDesired: i + 1}); err != nil {
			t.Fatal(err)
		}
	}
	pts, err := h.RawPointsSince(0, 100)
	if err != nil || len(pts) != 1 || pts[0].RawDesired != 3 {
		t.Fatalf("got %+v err=%v, want one point value 3", pts, err)
	}
}
