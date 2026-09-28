package accept

import (
	"context"
	"time"
)

// Scenario 1: a clean positive load step on a fully reporting fleet.
//
//	3 fresh * 200 = 600 -> ceil(600/100) = 6 (within rate cap 6) => scale up.
func scenLoadStep(ctx context.Context, s *session) error {
	s.reportAll(200, 0)
	s.reconcile(ctx, "step-up-to-6") // expect scale_up 3 -> 6
	return nil
}

// Scenario 2: a huge step is bounded by the independently maintained scale-up
// rate limit. 2 fresh * 500 = 1000 -> raw desired 10, ceiling
// min(ceil(2*2)=4, 2+4=6) = 4.
func scenRateCap(ctx context.Context, s *session) error {
	s.reportAll(500, 0)
	s.reconcile(ctx, "rate-capped-4") // expect scale_up 2 -> 4, SCALE_UP_RATE_LIMITED
	return nil
}

// Scenario 3: one instance fails to report. Conservative imputation at target
// load (100) plus the hard missing-guard forbid a shrink even though the two
// reporters look lightly loaded.
func scenMissing(ctx context.Context, s *session) error {
	s.reportExcept(10, 0, "ins-0002") // measured 20 + imputed 100 = 120
	s.reconcile(ctx, "missing-hold")  // expect none at 3, MISSING_INSTANCE_BLOCKS_DOWNSCALE
	return nil
}

// Scenario 4: delayed / stale reporting. High values arrive but are already
// older than the 3s freshness horizon, so they must not trigger a scale up.
func scenDelayedStale(ctx context.Context, s *session) error {
	s.reportAll(900, 5*time.Second) // high but expired
	s.reconcile(ctx, "stale-hold")  // expect none at 3, ALL_METRICS_STALE_HOLD
	return nil
}

// Scenario 5: a short load spike grows capacity (rate capped); the immediate
// dip cannot shrink instantly; after the 3s stable window the shrink applies.
// Then the real process is restarted against the same DB: fleet size, stable
// IDs and the (non-reused) allocation sequence must survive.
func scenSpikeRestart(ctx context.Context, s *session) error {
	// Spike: 3 * 300 = 900 -> raw 9, cap to min(ceil(6),7)=6.
	s.reportAll(300, 0)
	s.reconcile(ctx, "spike-up-6") // expect scale_up 3 -> 6

	// Immediate dip: first low reading, window not satisfied -> hold.
	s.reportAll(10, 0)
	s.reconcile(ctx, "dip-hold-6") // expect none at 6, first-observation defer

	// Wait beyond the stable window while staying low. Two readings spanning
	// >=3s both recommend min floor 1 -> shrink 6 -> 1.
	time.Sleep(3200 * time.Millisecond)
	s.reportAll(10, 0)
	s.reconcile(ctx, "shrink-to-1") // expect scale_down 6 -> 1

	// --- real service restart on the same database file ---
	if err := s.srv.restart(ctx); err != nil {
		return err
	}
	// The fleet must still be exactly the lowest-sequenced single instance.
	n, ids := s.getFleet()
	if n != 1 || len(ids) != 1 || ids[0] != "ins-0001" {
		s.out.record(s.scenario, "restart-fleet", "restart", n, 1, nil, false,
			"after restart expected exactly [ins-0001], got "+joinIDs(ids))
	} else {
		s.out.record(s.scenario, "restart-fleet", "restart", n, 1, nil, true,
			"fleet persisted across real process restart")
	}

	// Grow after restart: sequence must continue (no ID reuse).
	s.reportAll(300, 0)
	s.reconcile(ctx, "post-restart-grow") // 1 * 300 -> ceil(3)=3, cap min(2,5)=2
	_, ids2 := s.getFleet()
	want := []string{"ins-0001", "ins-0007"} // ins-0002..0006 were allocated in the spike
	if !equalIDs(ids2, want) {
		s.out.record(s.scenario, "post-restart-ids", "restart", int32(len(ids2)), 2, nil, false,
			"allocation sequence must continue without ID reuse; got "+joinIDs(ids2)+" want "+joinIDs(want))
	} else {
		s.out.record(s.scenario, "post-restart-ids", "restart", 2, 2, nil, true,
			"new instance is ins-0007: IDs are not reused after restart")
	}
	return nil
}

// Scenario 6: the independent scale-from-zero policy. At zero with no demand
// the controller holds; an expired demand still holds; a fresh positive
// demand bootstraps exactly one replica.
func scenFromZero(ctx context.Context, s *session) error {
	s.reconcile(ctx, "zero-no-demand") // expect none at 0

	s.setDemand(4, time.Now().Add(-5*time.Second))
	s.reconcile(ctx, "zero-expired-demand") // expect none at 0

	s.setDemand(7, time.Now())
	s.reconcile(ctx, "zero-bootstrap") // expect scale_up 0 -> 1
	return nil
}
