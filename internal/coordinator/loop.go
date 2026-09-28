package coordinator

import (
	"context"
	"sync"
	"time"

	"evictor/internal/logx"
)

// Loop is the background reconciliation ticker. It exists so approvals that
// pause after approval, instances that fail first, and completed moves are
// all settled against real observations without a caller poking the API.
type Loop struct {
	c       *Coordinator
	groups  func() []string
	tick    time.Duration
	log     *logx.Logger

	wg     sync.WaitGroup
	cancel context.CancelFunc
}

// NewLoop builds a ticker. groups is queried each tick so groups created
// after startup are picked up without a restart.
func (c *Coordinator) NewLoop(groups func() []string) *Loop {
	return &Loop{c: c, groups: groups, tick: c.cfg.Interval, log: c.log}
}

// Start launches the loop and returns immediately.
func (l *Loop) Start(ctx context.Context) {
	ctx, l.cancel = context.WithCancel(ctx)
	l.wg.Add(1)
	go func() {
		defer l.wg.Done()
		t := time.NewTicker(l.tick)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				l.pass(ctx)
			}
		}
	}()
}

// RunOne performs exactly one pass synchronously (used by tests and by the
// /admin/reconcile endpoint).
func (l *Loop) RunOne(ctx context.Context) { l.pass(ctx) }

func (l *Loop) pass(ctx context.Context) {
	for _, g := range l.groups() {
		if err := l.c.Reconcile(ctx, g); err != nil {
			l.log.Event("error", "reconcile-failed", "",
				logx.F("group", g), logx.F("err", err.Error()))
		}
	}
}

// Stop halts the loop and waits for the in-flight pass to finish.
func (l *Loop) Stop() {
	if l.cancel != nil {
		l.cancel()
	}
	l.wg.Wait()
}
