// Package reconcile runs the controller loop: every successful apply enqueues
// a resource, a worker fetches the latest merged live state and drives the
// Adapter, then persists synced/failed status with bounded exponential
// backoff. The queue is bounded: a saturated enqueue returns a typed
// resource_exhausted error so callers can distinguish backpressure from data
// or state errors.
package reconcile

import (
	"context"
	"math/rand"
	"sync"
	"time"

	"fieldmerge/internal/adapter"
	"fieldmerge/internal/apperr"
	"fieldmerge/internal/log"
	"fieldmerge/internal/store"
)

type ref struct{ kind, name string }

type Config struct {
	QueueSize      int
	Workers        int
	MaxAttempts    int
	BaseBackoff    time.Duration
	MaxBackoff     time.Duration
	DuePollEvery   time.Duration
	QueueBatchSize int
}

func DefaultConfig() Config {
	return Config{
		QueueSize:      128,
		Workers:        2,
		MaxAttempts:    5,
		BaseBackoff:    100 * time.Millisecond,
		MaxBackoff:     30 * time.Second,
		DuePollEvery:   2 * time.Second,
		QueueBatchSize: 32,
	}
}

// Loop is the reconciliation worker pool.
type Loop struct {
	st        *store.Store
	ad        adapter.Adapter
	cfg       Config
	logger    *logx.Logger
	queue     chan ref
	wg        sync.WaitGroup
	rng       *rand.Rand
	enqueueMu sync.Mutex
	queued    map[string]struct{}
}

// New creates a Loop (call Start to launch workers).
func New(st *store.Store, ad adapter.Adapter, cfg Config, logger *logx.Logger) *Loop {
	if cfg.QueueSize <= 0 {
		cfg = DefaultConfig()
	}
	if cfg.Workers <= 0 {
		cfg.Workers = 1
	}
	if cfg.MaxAttempts <= 0 {
		cfg.MaxAttempts = 5
	}
	if cfg.BaseBackoff <= 0 {
		cfg.BaseBackoff = 100 * time.Millisecond
	}
	if cfg.MaxBackoff <= 0 {
		cfg.MaxBackoff = 30 * time.Second
	}
	if cfg.DuePollEvery <= 0 {
		cfg.DuePollEvery = 2 * time.Second
	}
	if cfg.QueueBatchSize <= 0 {
		cfg.QueueBatchSize = 32
	}
	return &Loop{
		st: st, ad: ad, cfg: cfg, logger: logger,
		queue:  make(chan ref, cfg.QueueSize),
		queued: map[string]struct{}{},
		rng:    rand.New(rand.NewSource(time.Now().UnixNano())),
	}
}

// Enqueue adds a resource to the immediate queue. Returns a typed
// resource_exhausted/queue_full error when the bounded queue is saturated.
// Duplicate enqueues while an item is already waiting are coalesced.
func (l *Loop) Enqueue(kind, name string) error {
	key := kind + "/" + name
	l.enqueueMu.Lock()
	if _, waiting := l.queued[key]; waiting {
		l.enqueueMu.Unlock()
		return nil
	}
	select {
	case l.queue <- ref{kind: kind, name: name}:
		l.queued[key] = struct{}{}
		l.enqueueMu.Unlock()
		return nil
	default:
		l.enqueueMu.Unlock()
		return apperr.New(apperr.ResourceExhausted, "queue_full",
			"reconcile queue full (%d pending); resource will be picked up by the due poll",
			l.cfg.QueueSize)
	}
}

// QueueLen reports pending immediate-work items (diagnostics).
func (l *Loop) QueueLen() int { return len(l.queue) }

// Start launches workers and the due-poll loop.
func (l *Loop) Start(ctx context.Context) {
	for i := 0; i < l.cfg.Workers; i++ {
		l.wg.Add(1)
		go l.worker(ctx, i)
	}
	l.wg.Add(1)
	go l.duePoll(ctx)
}

// Stop waits for workers to drain.
func (l *Loop) Stop() {
	l.wg.Wait()
}

func (l *Loop) worker(ctx context.Context, id int) {
	defer l.wg.Done()
	for {
		select {
		case <-ctx.Done():
			return
		case r := <-l.queue:
			l.enqueueMu.Lock()
			delete(l.queued, r.kind+"/"+r.name)
			l.enqueueMu.Unlock()
			l.reconcileOne(ctx, r)
		}
	}
}

func (l *Loop) duePoll(ctx context.Context) {
	defer l.wg.Done()
	t := time.NewTicker(l.cfg.DuePollEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			due, err := l.st.DueResources(ctx, time.Now(), l.cfg.QueueBatchSize)
			if err != nil {
				l.logger.Warn("due_poll_failed", map[string]any{"err": err.Error()})
				continue
			}
			for _, r := range due {
				_ = l.Enqueue(r.Kind, r.Name) // full queue: next poll retries
			}
		}
	}
}

// ReconcileOne runs one sync for tests / synchronous use.
func (l *Loop) ReconcileOne(ctx context.Context, kind, name string) error {
	r, err := l.st.Get(ctx, kind, name)
	if err != nil {
		return err
	}
	if r == nil {
		return apperr.New(apperr.NotFound, "resource_missing", "%s/%s not found", kind, name)
	}
	l.reconcileOneWith(ctx, r)
	return nil
}

func (l *Loop) reconcileOne(ctx context.Context, r ref) {
	res, err := l.st.Get(ctx, r.kind, r.name)
	if err != nil {
		l.logger.Error("reconcile_get_failed", map[string]any{"kind": r.kind, "name": r.name, "err": err.Error()})
		return
	}
	if res == nil {
		return // deleted between enqueue and processing
	}
	l.reconcileOneWith(ctx, res)
}

func (l *Loop) reconcileOneWith(ctx context.Context, res *store.Resource) {
	l.logger.Info("reconcile_start", map[string]any{
		"kind": res.Kind, "name": res.Name, "revision": res.Revision,
		"attempt": res.Attempts + 1, "adapter": l.ad.Name(),
	})

	out := l.ad.Apply(ctx, adapter.DesiredState{
		Kind: res.Kind, Name: res.Name, Revision: res.Revision, Live: res.Live,
	})

	switch {
	case out.Synced:
		if err := l.st.MarkReconcile(ctx, res.Kind, res.Name, store.ReconcileResult{
			Status: "synced",
		}); err != nil {
			l.logger.Error("mark_synced_failed", map[string]any{"err": err.Error()})
			return
		}
		l.logger.Info("reconcile_synced", map[string]any{
			"kind": res.Kind, "name": res.Name, "revision": res.Revision,
		})
	case !out.Retryable || res.Attempts+1 >= l.cfg.MaxAttempts:
		reason := out.Reason
		if !out.Retryable {
			reason = "fatal:" + reason
		}
		_ = l.st.MarkReconcile(ctx, res.Kind, res.Name, store.ReconcileResult{
			Status: "failed", Error: reason + ": " + out.Message,
		})
		l.logger.Error("reconcile_failed_permanent", map[string]any{
			"kind": res.Kind, "name": res.Name, "attempts": res.Attempts + 1,
			"reason": out.Reason, "message": out.Message,
		})
	default:
		backoff := l.backoff(res.Attempts + 1)
		next := time.Now().Add(backoff)
		_ = l.st.MarkReconcile(ctx, res.Kind, res.Name, store.ReconcileResult{
			Status: "failed", Error: out.Reason + ": " + out.Message, NextRetry: next,
		})
		l.logger.Warn("reconcile_retry_scheduled", map[string]any{
			"kind": res.Kind, "name": res.Name, "attempt": res.Attempts + 1,
			"reason": out.Reason, "backoff_ms": backoff.Milliseconds(),
		})
	}
}

// backoff = min(max, base * 2^(n-1)) with +/-20% jitter.
func (l *Loop) backoff(attempt int) time.Duration {
	d := l.cfg.BaseBackoff
	for i := 1; i < attempt && d < l.cfg.MaxBackoff; i++ {
		d *= 2
		if d > l.cfg.MaxBackoff {
			d = l.cfg.MaxBackoff
		}
	}
	f := 0.8 + 0.4*l.rng.Float64()
	d = time.Duration(float64(d) * f)
	if d < l.cfg.BaseBackoff {
		d = l.cfg.BaseBackoff
	}
	return d
}
