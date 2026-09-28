package adapter

import (
	"errors"
	"testing"

	"replicactl/core/model"
)

func TestFixtureSlotIdentityAndMissingClassification(t *testing.T) {
	cfg := model.DefaultConfig()
	f := NewLocalFixture(cfg, 2)
	f.Clock = func() int64 { return 1000 }

	got := f.ActiveInstances()
	if len(got) != 2 || got[0] != "instance-001" || got[1] != "instance-002" {
		t.Fatalf("active instances %v want [instance-001 instance-002]", got)
	}

	samples, err := f.LatestSamples(1000)
	if err != nil {
		t.Fatal(err)
	}
	if len(samples) != 2 || !samples[0].Missing || !samples[1].Missing {
		t.Fatalf("fresh fleet must read as two missing samples, got %+v", samples)
	}

	if err := f.SubmitSample(model.LoadSample{InstanceID: "instance-001", Load: 12, ReportedAt: 1000}, 1000); err != nil {
		t.Fatal(err)
	}
	// A report for an out-of-range slot is rejected (identity boundary).
	if err := f.SubmitSample(model.LoadSample{InstanceID: "instance-003", Load: 1, ReportedAt: 1000}, 1000); err == nil {
		t.Fatal("expected rejection for inactive slot")
	}
	// Future-dated and negative-load reports are rejected at the boundary.
	if err := f.SubmitSample(model.LoadSample{InstanceID: "instance-001", Load: 1, ReportedAt: 1001}, 1000); err == nil {
		t.Fatal("expected rejection for future timestamp")
	}
	if err := f.SubmitSample(model.LoadSample{InstanceID: "instance-001", Load: -1, ReportedAt: 1000}, 1000); err == nil {
		t.Fatal("expected rejection for negative load")
	}

	// Scale down to zero and back up: slot identities retained; the second
	// slot starts missing again (realistic cold behaviour).
	if err := f.SetReplicas(0); err != nil {
		t.Fatal(err)
	}
	if err := f.SetReplicas(2); err != nil {
		t.Fatal(err)
	}
	samples, _ = f.LatestSamples(1000)
	if samples[0].Missing || samples[1].Missing != true {
		t.Fatalf("after reactivation i1 should retain its report, i2 be missing: %+v %+v", samples[0], samples[1])
	}
}

func TestFixtureFaultHooks(t *testing.T) {
	cfg := model.DefaultConfig()
	f := NewLocalFixture(cfg, 1)
	boom := errors.New("boom")
	f.FailMetricRead = boom
	if _, err := f.LatestSamples(1); !errors.Is(err, boom) {
		t.Fatalf("metric hook: %v", err)
	}
	f.ClearFaults()
	if _, err := f.LatestSamples(1); err != nil {
		t.Fatalf("cleared hook still fails: %v", err)
	}
	f.FailSetReplicas = boom
	if err := f.SetReplicas(2); !errors.Is(err, boom) {
		t.Fatalf("set hook: %v", err)
	}
	f.FailDemandRead = boom
	if _, _, err := f.LatestDemand(1); !errors.Is(err, boom) {
		t.Fatalf("demand hook: %v", err)
	}
}

func TestFixtureOutOfRangeReject(t *testing.T) {
	cfg := model.DefaultConfig()
	f := NewLocalFixture(cfg, 2)
	if err := f.SetReplicas(99); err == nil {
		t.Fatal("expected out-of-range rejection")
	}
}
