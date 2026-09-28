package plugins

import (
	"context"
	"sync"
	"time"

	"admission/internal/model"
)

// ScriptedQuota is the deterministic QuotaService fixture. Tests script each
// UID's outcome: exhaustion, a compute error, or a slow response that exceeds
// the plugin timeout. It also records calls so duplicate-call idempotency can
// be asserted.
type ScriptedQuota struct {
	mu       sync.Mutex
	reserves map[string]int
	releases map[string]int
	// Behavior maps UID -> canned behavior.
	Behavior map[string]ScriptKind
	Timeout  time.Duration
}

// NewScriptedQuota builds an empty fixture; set Behavior entries per test.
func NewScriptedQuota() *ScriptedQuota {
	return &ScriptedQuota{
		reserves: map[string]int{},
		releases: map[string]int{},
		Behavior: map[string]ScriptKind{},
	}
}

func (q *ScriptedQuota) Reserve(ctx context.Context, uid, _ string, _ int64, _ model.Operation) error {
	q.mu.Lock()
	q.reserves[uid]++
	kind := q.Behavior[uid]
	d := q.Timeout
	q.mu.Unlock()
	switch kind {
	case ScriptTimeout:
		if d <= 0 {
			d = time.Millisecond
		}
		select {
		case <-time.After(2 * d):
		case <-ctx.Done():
			return ctx.Err()
		}
		return ctx.Err()
	case ScriptComputeError:
		return Fail(model.ReasonComputeFailure, "scripted quota adapter failure")
	case ScriptExhausted:
		return ErrQuotaExhausted
	}
	return nil
}

func (q *ScriptedQuota) Release(_ context.Context, uid string) error {
	q.mu.Lock()
	q.releases[uid]++
	q.mu.Unlock()
	return nil
}

// Exhaust marks uid so its reserve returns the quota-exhausted signal.
func (q *ScriptedQuota) Exhaust(uid string) { q.SetBehavior(uid, ScriptExhausted) }

// SetBehavior assigns a canned behavior for uid.
func (q *ScriptedQuota) SetBehavior(uid string, kind ScriptKind) {
	q.mu.Lock()
	q.Behavior[uid] = kind
	q.mu.Unlock()
}

// ReserveCount reports how many reserves were attempted for uid.
func (q *ScriptedQuota) ReserveCount(uid string) int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.reserves[uid]
}

// ReleaseCount reports how many releases were attempted for uid.
func (q *ScriptedQuota) ReleaseCount(uid string) int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.releases[uid]
}
