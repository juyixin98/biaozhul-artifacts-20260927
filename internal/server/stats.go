package server

import (
	"context"
	"time"

	"dhcpv4lab/internal/storage"
)

// Counters is a snapshot of process-wide decision counters.
type Counters struct {
	Accepted uint64 `json:"accepted"`
	Rejected uint64 `json:"rejected"`
	Replayed uint64 `json:"replayed"`
}

// Counters returns the current counter snapshot.
func (s *Server) Counters() Counters {
	return Counters{
		Accepted: s.accepted.Load(),
		Rejected: s.rejected.Load(),
		Replayed: s.replayed.Load(),
	}
}

// Sweep runs one expiry pass and returns the applied transitions.
func (s *Server) Sweep(ctx context.Context, runID string) ([]storage.SweepChange, error) {
	now := s.clock.Now()
	changes, err := s.store.SweepExpired(ctx, now.UnixNano())
	if err != nil {
		return nil, err
	}
	for _, ch := range changes {
		verb := "offer_expired"
		if ch.From == storage.StateBound {
			verb = "lease_expired"
		}
		_, _ = s.store.InsertEvent(ctx, storage.Event{
			RunID:      orEmpty(runID),
			TsNanos:    now.UnixNano(),
			IdentityID: ch.IdentityID,
			InType:     "TIMER",
			Action:     verb,
			Result:     "ok",
			Reason:     "deadline_reached",
			AssignedIP: ch.IP,
			Detail:     string(ch.From) + " -> EXPIRED at " + now.Format(time.RFC3339Nano),
		})
	}
	return changes, nil
}

// StartSweeper launches the background expiry loop until ctx is canceled.
func (s *Server) StartSweeper(ctx context.Context, interval time.Duration, runID string) {
	if interval <= 0 {
		return
	}
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				_, _ = s.Sweep(ctx, runID)
			}
		}
	}()
}
