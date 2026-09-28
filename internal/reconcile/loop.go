package reconcile

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

// Loop drives Reconciler.Once on a ticker and on explicit triggers.
type Loop struct {
	rec      *Reconciler
	interval time.Duration
	log      *slog.Logger
	trigger  chan struct{}

	mu      sync.RWMutex
	last    RunRecord
	hasLast bool
}

// NewLoop builds a polling loop. interval <= 0 disables periodic polling,
// leaving only explicit triggers.
func NewLoop(rec *Reconciler, interval time.Duration, log *slog.Logger) *Loop {
	if log == nil {
		log = slog.Default()
	}
	return &Loop{
		rec:      rec,
		interval: interval,
		log:      log,
		trigger:  make(chan struct{}, 1),
	}
}

// Trigger requests an immediate out-of-band pass. Signals coalesce: a
// pending trigger is not queued twice.
func (l *Loop) Trigger() {
	select {
	case l.trigger <- struct{}{}:
	default:
	}
}

// Last returns the most recent run record and whether one has happened.
// It is safe to call concurrently with a running loop.
func (l *Loop) Last() (RunRecord, bool) {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return l.last, l.hasLast
}

// Run blocks until ctx is canceled. An initial pass runs immediately so the
// service serves state without waiting one interval.
func (l *Loop) Run(ctx context.Context) {
	l.runOnce(ctx)

	var tick <-chan time.Time
	if l.interval > 0 {
		ticker := time.NewTicker(l.interval)
		defer ticker.Stop()
		tick = ticker.C
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick:
			l.runOnce(ctx)
		case <-l.trigger:
			l.runOnce(ctx)
		}
	}
}

func (l *Loop) runOnce(ctx context.Context) {
	rec, err := l.rec.Once(ctx)
	if err != nil {
		l.log.Error("reconcile pass could not be recorded", "error", err)
		return
	}
	l.mu.Lock()
	l.last, l.hasLast = rec, true
	l.mu.Unlock()
	attrs := []any{"status", rec.Status, "revision", rec.Revision}
	if rec.ContentHash != "" {
		attrs = append(attrs, "hash", rec.ContentHash)
	}
	if rec.ErrorKind != "" {
		attrs = append(attrs, "errorKind", rec.ErrorKind)
	}
	switch rec.Status {
	case StatusApplied, StatusUnchanged:
		l.log.Info("reconcile pass complete", attrs...)
	default:
		l.log.Warn("reconcile pass failed", attrs...)
	}
}
