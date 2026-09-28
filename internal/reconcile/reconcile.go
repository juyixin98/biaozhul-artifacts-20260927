// Package reconcile implements the bounded-retry control loop: requests whose
// failure category is retryable (timeout, quota exhaustion, compute failure)
// are re-admitted with exponential backoff up to a configured attempt cap;
// input errors, illegal mutations, state conflicts and clean denials are
// terminal and are never retried.
package reconcile

import (
	"context"
	"sync"
	"time"

	"admission/internal/service"
	"admission/internal/storage"
	"admission/internal/types"
)

// Policy is the retry policy.
type Policy struct {
	MaxAttempts int           // total attempts including the first
	BaseDelay   time.Duration // delay before attempt 2
	Factor      float64       // exponential factor per later attempt
	MaxDelay    time.Duration // cap
}

// DefaultPolicy is used when fields are zero.
func DefaultPolicy() Policy {
	return Policy{MaxAttempts: 4, BaseDelay: 50 * time.Millisecond, Factor: 2, MaxDelay: time.Second}
}

// AttemptOutcome reports what one Tick achieved.
type AttemptOutcome struct {
	UID      string
	RunID    string
	Attempt  int
	Allowed  bool
	Replayed bool
	Category types.Category
	Retrying bool
	Dropped  bool // attempts exhausted; left terminal in store/audit
	Idle     bool // nothing was due
}

// Reconciler owns the retry queue in storage. Clocks are injectable so tests
// advance time deterministically without sleeping.
type Reconciler struct {
	svc    *service.Service
	store  storage.Store
	policy Policy
	now    func() time.Time
	newID  func() string

	mu      sync.Mutex
	lastRun map[string]string // uid -> run id (for test inspection)
}

// New constructs a reconciler.
func New(svc *service.Service, store storage.Store, policy Policy,
	now func() time.Time, newID func() string) *Reconciler {
	if policy.MaxAttempts <= 0 {
		policy = DefaultPolicy()
	}
	if now == nil {
		now = time.Now
	}
	if newID == nil {
		newID = func() string { return "" }
	}
	return &Reconciler{svc: svc, store: store, policy: policy, now: now, newID: newID,
		lastRun: map[string]string{}}
}

// Submit records the original review for possible retry with attempt number n
// scheduled at notBefore. The HTTP path runs attempt 1 synchronously and only
// calls ScheduleRetry afterwards, so the background loop can never race the
// first attempt.
func (r *Reconciler) Submit(ctx context.Context, req types.Review) error {
	return r.store.EnqueueRetry(ctx, req, 1, r.now().UnixNano())
}

// ScheduleRetry enqueues a retry for a request whose synchronous attempt just
// failed with a retryable category (attempt = next, due after backoff). A
// terminal verdict schedules nothing.
func (r *Reconciler) ScheduleRetry(ctx context.Context, req types.Review, lastAttempt int) error {
	next := lastAttempt + 1
	if next > r.policy.MaxAttempts {
		return nil
	}
	return r.store.EnqueueRetry(ctx, req, next, r.now().Add(r.delayFor(next)).UnixNano())
}

// Tick processes at most one due item. It is both the body of the background
// loop and the deterministic entry point tests use.
func (r *Reconciler) Tick(ctx context.Context) AttemptOutcome {
	item, err := r.store.DequeueRetry(ctx, r.now().UnixNano())
	if err == storage.ErrNotFound {
		return AttemptOutcome{Idle: true}
	}
	if err != nil {
		return AttemptOutcome{Idle: true, Category: types.CatComputeFailure}
	}

	// A stored verdict can appear while the item was parked: honor idempotency.
	if prior, ok, _ := r.store.LookupUID(ctx, item.UID); ok {
		_ = r.store.DoneRetry(ctx, item.UID)
		return AttemptOutcome{UID: item.UID, Attempt: item.Attempts, Allowed: prior.Allowed, Replayed: true}
	}

	runID := r.newID()
	resp := r.svc.Admit(ctx, runID, item.Review, item.Attempts)
	r.mu.Lock()
	r.lastRun[item.UID] = resp.RunID
	r.mu.Unlock()

	out := AttemptOutcome{
		UID: item.UID, RunID: resp.RunID, Attempt: item.Attempts,
		Allowed: resp.Allowed, Replayed: resp.Replayed, Category: resp.FailureCategory,
	}

	if resp.Allowed || resp.Replayed {
		_ = r.store.DoneRetry(ctx, item.UID)
		return out
	}
	next := item.Attempts + 1
	if !resp.FailureCategory.Retryable() || next > r.policy.MaxAttempts {
		_ = r.store.DoneRetry(ctx, item.UID)
		out.Dropped = !resp.FailureCategory.Retryable() || next > r.policy.MaxAttempts
		return out
	}
	_ = r.store.RequeueRetry(ctx, item.UID, next, r.now().Add(r.delayFor(next)).UnixNano())
	out.Retrying = true
	return out
}

// RunUntilIdle is a test/helper loop that ticks until the queue drains or
// maxTicks is reached (guards against accidental loops).
func (r *Reconciler) RunUntilIdle(ctx context.Context, maxTicks int) []AttemptOutcome {
	var outs []AttemptOutcome
	for i := 0; i < maxTicks; i++ {
		o := r.Tick(ctx)
		if o.Idle {
			break
		}
		outs = append(outs, o)
	}
	return outs
}

// LastRun returns the last run id used for a uid.
func (r *Reconciler) LastRun(uid string) string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.lastRun[uid]
}

// delayFor returns the backoff before attempt number n (n >= 2).
func (r *Reconciler) delayFor(n int) time.Duration {
	if n < 2 {
		return 0
	}
	d := float64(r.policy.BaseDelay)
	for i := 2; i < n; i++ {
		d *= r.policy.Factor
	}
	if r.policy.MaxDelay > 0 && d > float64(r.policy.MaxDelay) {
		d = float64(r.policy.MaxDelay)
	}
	return time.Duration(d)
}

// Start runs the background loop until ctx is cancelled. Ticks happen on a
// short fixed cadence; backoff is expressed through not_before timestamps.
func (r *Reconciler) Start(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = 25 * time.Millisecond
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			r.Tick(ctx)
		}
	}
}
