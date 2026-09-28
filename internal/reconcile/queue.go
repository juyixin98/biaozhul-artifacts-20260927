package reconcile

import (
	"sync"
	"time"
)

// Queue is a deduplicating, single-worker work queue with per-key exponential
// backoff. Repeated Add calls for a key that is already queued or in
// processing collapse into one future processing (the key is marked dirty),
// which is how duplicate events are coalesced.
//
// The design mirrors the classic Kubernetes client-go workqueue semantics
// (Add/Done/AddLimited/ShutDown) but is intentionally small and synchronous so
// tests can observe deterministic counts.
type Queue struct {
	mu         sync.Mutex
	cond       *sync.Cond
	dirty      map[string]struct{} // keys needing processing
	processing map[string]struct{}
	ready      []string
	delay      map[string]time.Time // earliest readyAt for delayed keys
	timers     map[string]*time.Timer
	backoff    Backoff
	stopped    bool
	wakeCh     chan struct{}
}

// Backoff configures retry pacing.
type Backoff struct {
	Base time.Duration
	Max  time.Duration
}

// NewQueue builds a queue.
func NewQueue(b Backoff) *Queue {
	q := &Queue{
		dirty:      map[string]struct{}{},
		processing: map[string]struct{}{},
		delay:      map[string]time.Time{},
		timers:     map[string]*time.Timer{},
		backoff:    b,
		wakeCh:     make(chan struct{}, 1),
	}
	q.cond = sync.NewCond(&q.mu)
	return q
}

// Add enqueues a key for immediate processing. If the key is already queued
// or is currently being processed, the call marks it dirty and returns: a
// follow-up processing is guaranteed, but duplicate adds never create
// duplicate work items.
func (q *Queue) Add(key string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.stopped {
		return
	}
	if _, ok := q.processing[key]; ok {
		// Currently being processed: mark dirty so the next Add after Done
		// requeues exactly once.
		q.dirty[key] = struct{}{}
		return
	}
	if _, ok := q.dirty[key]; ok {
		return // already due
	}
	// Cancel any pending backoff timer: a fresh event makes the key due now.
	if t, ok := q.timers[key]; ok {
		t.Stop()
		delete(q.timers, key)
		delete(q.delay, key)
	}
	q.dirty[key] = struct{}{}
	q.ready = append(q.ready, key)
	q.cond.Broadcast()
}

// Get blocks until a key is ready, then moves it from dirty to processing.
// Returns ("", false) after shutdown drains.
func (q *Queue) Get() (string, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for len(q.ready) == 0 {
		if q.stopped && len(q.dirty) == 0 {
			return "", false
		}
		q.cond.Wait()
	}
	key := q.ready[0]
	q.ready = q.ready[1:]
	delete(q.dirty, key)
	q.processing[key] = struct{}{}
	return key, true
}

// Done finishes a processing attempt. If the key accumulated duplicate adds
// during processing it is requeued immediately; otherwise it leaves the
// processing set.
func (q *Queue) Done(key string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	delete(q.processing, key)
	if q.stopped {
		q.cond.Broadcast()
		return
	}
	// Duplicate adds during processing set dirty but did not append to ready.
	if _, stillDirty := q.dirty[key]; stillDirty {
		// dirty entry remains; put back into ready.
		q.ready = append(q.ready, key)
		q.cond.Broadcast()
	}
}

// AddAfter requeues a key after d. It is used for expected multi-round
// transitions (not failures): it does not consult backoff and does not cancel
// a sooner due time.
func (q *Queue) AddAfter(key string, d time.Duration) {
	q.schedule(key, d, false)
}

// AddRateLimited requeues a key after an exponentially growing backoff based
// on its retry count.
func (q *Queue) AddRateLimited(key string, retries int) {
	d := q.backoff.delay(retries)
	q.schedule(key, d, true)
}

func (q *Queue) schedule(key string, d time.Duration, keepSooner bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.stopped {
		return
	}
	// Fresh events or an already-scheduled sooner timer take precedence; do
	// not push a due key into the future.
	if _, ok := q.dirty[key]; ok {
		return
	}
	if keepSooner {
		if existing, ok := q.delay[key]; ok && time.Until(existing) < d {
			return
		}
	}
	if t, ok := q.timers[key]; ok {
		t.Stop()
	}
	at := time.Now().Add(d)
	q.delay[key] = at
	t := time.AfterFunc(d, func() {
		q.mu.Lock()
		defer q.mu.Unlock()
		delete(q.timers, key)
		delete(q.delay, key)
		if q.stopped {
			return
		}
		if _, processing := q.processing[key]; processing {
			// Will be picked up via dirty on Done.
			q.dirty[key] = struct{}{}
			return
		}
		if _, already := q.dirty[key]; !already {
			q.dirty[key] = struct{}{}
			q.ready = append(q.ready, key)
			q.cond.Broadcast()
		}
	})
	q.timers[key] = t
}

// ShutDown stops delivery. In-flight work still completes.
func (q *Queue) ShutDown() {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.stopped = true
	for _, t := range q.timers {
		t.Stop()
	}
	q.timers = map[string]*time.Timer{}
	q.cond.Broadcast()
}

// Stats are queue counters for tests/diagnostics.
type Stats struct {
	Dirty      int
	Ready      int
	Processing int
	Delayed    int
}

// Stats returns a point-in-time snapshot.
func (q *Queue) Stats() Stats {
	q.mu.Lock()
	defer q.mu.Unlock()
	return Stats{
		Dirty:      len(q.dirty),
		Ready:      len(q.ready),
		Processing: len(q.processing),
		Delayed:    len(q.delay),
	}
}

func (b Backoff) delay(retries int) time.Duration {
	if b.Base <= 0 {
		b.Base = 10 * time.Millisecond
	}
	if b.Max <= 0 {
		b.Max = time.Second
	}
	d := b.Base
	for i := 0; i < retries; i++ {
		d *= 2
		if d >= b.Max {
			return b.Max
		}
	}
	if d > b.Max {
		return b.Max
	}
	return d
}
