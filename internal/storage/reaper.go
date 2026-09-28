package storage

import (
	"context"
	"log/slog"
	"time"
)

// StartReaper runs Sweep on the configured interval until ctx is
// cancelled. It is a belt-and-braces reclaimer: every mutating
// transaction already performs "sweep on touch", so this goroutine only
// matters when traffic is idle.
func (s *Store) StartReaper(ctx context.Context, interval time.Duration, log *slog.Logger) {
	if log == nil {
		log = slog.Default()
	}
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				o, l, err := s.Sweep(ctx)
				if err != nil {
					if ctx.Err() != nil {
						return
					}
					log.LogAttrs(ctx, slog.LevelWarn, "lease sweep failed",
						slog.String("component", "reaper"),
						slog.Any("err", err))
					continue
				}
				if o > 0 || l > 0 {
					log.LogAttrs(ctx, slog.LevelInfo, "expired state reclaimed",
						slog.String("component", "reaper"),
						slog.Int("offers", o), slog.Int("leases", l))
				}
			}
		}
	}()
}
