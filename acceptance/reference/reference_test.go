package reference_test

import (
	"testing"

	"replicactl/acceptance/reference"
)

// These checks pin the INDEPENDENT reference to hand-computed numbers from the
// behavioural contract (docs/ALGORITHM.md). They do not import production code.
func TestReferenceHandArithmetic(t *testing.T) {
	p := reference.DefaultParams()

	t.Run("load step rate limited", func(t *testing.T) {
		s := reference.NewSim(p, 2)
		s.PutSample("instance-001", 30, 1000, 1000)
		s.PutSample("instance-002", 30, 1000, 1000)
		o := s.Reconcile(1000)
		if o.Action != "scale_up" || o.DesiredReplicas != 4 || o.RawDesired != 6 || o.TotalLoad != 60 {
			t.Fatalf("got %+v", o)
		}
		if len(o.Reasons) != 1 || o.Reasons[0] != "UP_RATE_LIMITED" {
			t.Fatalf("reasons %v", o.Reasons)
		}
	})

	t.Run("stale storm does nothing", func(t *testing.T) {
		s := reference.NewSim(p, 4)
		s.PutSample("instance-001", 99, 969, 1000)
		s.PutSample("instance-002", 99, 969, 1000)
		o := s.Reconcile(1000)
		if o.Action != "noop" || o.Reasons[0] != "NO_FRESH_METRICS" || o.Stale != 2 || o.Missing != 2 {
			t.Fatalf("got %+v", o)
		}
		if o.TotalLoad != 40 {
			t.Fatalf("imputed total %v want 40 (4*T)", o.TotalLoad)
		}
	})

	t.Run("window maximum withholds downscale", func(t *testing.T) {
		// Two instances: idle at 1000..1050, spike at 1060, idle 1070..1120.
		s := reference.NewSim(p, 2)
		report := func(at int64, load float64) {
			s.PutSample("instance-001", load, at, at)
			s.PutSample("instance-002", load, at, at)
		}
		for tick := int64(1000); tick <= 1050; tick += 10 {
			report(tick, 0)
			if o := s.Reconcile(tick); o.Action != "noop" || o.Reasons[0] != "WINDOW_PENDING" {
				t.Fatalf("tick %d got %+v", tick, o)
			}
		}
		// tick 1060: downscale window covered and all zero -> scale to zero.
		report(1060, 0)
		if o := s.Reconcile(1060); o.Action != "scale_down" || o.DesiredReplicas != 0 {
			t.Fatalf("tick 1060 got %+v", o)
		}
	})

	t.Run("spike point stays in window", func(t *testing.T) {
		s := reference.NewSim(p, 2)
		report := func(at int64, load float64) {
			s.PutSample("instance-001", load, at, at)
			s.PutSample("instance-002", load, at, at)
		}
		for tick := int64(1000); tick <= 1050; tick += 10 {
			report(tick, 0)
			s.Reconcile(tick)
		}
		report(1060, 30) // raw 6 spike
		o := s.Reconcile(1060)
		if o.Action != "scale_up" {
			t.Fatalf("spike tick %+v", o)
		}
		// Fleet is now 6. Idle at 1070..1110: spike raw 6 < current 6 means
		// downscale to... window max is 6 >= current 6 => pending.
		for tick := int64(1070); tick <= 1120; tick += 10 {
			for i := 1; i <= 6; i++ {
				s.PutSample(instIDS(t, i), 0, tick, tick)
			}
			o := s.Reconcile(tick)
			if o.Action != "noop" || o.Reasons[0] != "WINDOW_PENDING" {
				t.Fatalf("tick %d got %+v, expected withheld by spike", tick, o)
			}
		}
	})

	t.Run("zero policies", func(t *testing.T) {
		s := reference.NewSim(p, 0)
		if o := s.Reconcile(1000); o.Reasons[0] != "ZERO_NO_DEMAND" {
			t.Fatalf("no demand: %+v", o)
		}
		s.PutDemand(true, 969)
		if o := s.Reconcile(1000); o.Reasons[0] != "ZERO_DEMAND_STALE" {
			t.Fatalf("stale demand: %+v", o)
		}
		s.PutDemand(true, 1000)
		if o := s.Reconcile(1000); o.Action != "scale_up" || o.DesiredReplicas != 1 {
			t.Fatalf("fresh demand: %+v", o)
		}
	})

	t.Run("export restore preserves window and fleet", func(t *testing.T) {
		s := reference.NewSim(p, 4)
		s.PutSample("instance-001", 5, 1000, 1000)
		s.PutSample("instance-002", 5, 1000, 1000)
		s.Reconcile(1000)
		st := s.Export()
		s2 := reference.Restore(p, st)
		if s2.Replicas() != 4 {
			t.Fatalf("restored replicas %d", s2.Replicas())
		}
		s2.PutSample("instance-001", 5, 1010, 1010)
		s2.PutSample("instance-002", 5, 1010, 1010)
		o := s2.Reconcile(1010)
		if o.RawDesired != 3 {
			t.Fatalf("history/state lost across restore: %+v", o)
		}
	})
}

// instIDS mirrors the production id scheme but is a local test helper.
func instIDS(t *testing.T, i int) string {
	t.Helper()
	return "instance-" + pad3(i)
}

func pad3(i int) string {
	if i >= 100 {
		return string(rune('0'+i/100)) + string(rune('0'+(i/10)%10)) + string(rune('0'+i%10))
	}
	if i >= 10 {
		return "0" + string(rune('0'+i/10)) + string(rune('0'+i%10))
	}
	return "00" + string(rune('0'+i))
}
