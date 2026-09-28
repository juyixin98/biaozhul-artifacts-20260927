package reconcile

import (
	"sync"
	"time"
)

// queue is a deduplicating work queue with per-key backoff, modelled on the
// work queue concept from Kubernetes controllers:
//
//   - Adding a key that is already queued or currently being processed does
//     not queue it twice (the "dirty" set collapses duplicate watch events).
//   - AddAfter schedules a retry after a delay (exponential backoff).
//   - Get blocks until work is available; Done marks an item finished and
//     re-adds it if it was marked dirty while it was in flight.
//   - Shutdown drains waiters; Get returns ok=false afterwards.
type queue struct {
	mu      sync.Mutex
	cond    *sync.Cond
	q       []string
	dirty   map[string]struct{} // queued or in-flight and wanted again
	working map[string]struct{} // handed out via Get, not yet Done
	timers  map[string]*time.Timer
	stopped bool
}

func newQueue() *queue {
	q := &queue{
		dirty:   map[string]struct{}{},
		working: map[string]struct{}{},
		timers:  map[string]*time.Timer{},
	}
	q.cond = sync.NewCond(&q.mu)
	return q
}

// Add enqueues key immediately (deduplicated).
func (q *queue) Add(key string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.stopped {
		return
	}
	if t, ok := q.timers[key]; ok {
		t.Stop()
		delete(q.timers, key)
	}
	if _, queued := q.dirty[key]; queued {
		return
	}
	q.dirty[key] = struct{}{}
	if _, busy := q.working[key]; busy {
		// It will be re-queued when Done is called.
		return
	}
	q.q = append(q.q, key)
	q.cond.Signal()
}

// AddAfter schedules key after delay. Used for retries with backoff.
func (q *queue) AddAfter(key string, delay time.Duration) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.stopped {
		return
	}
	if _, exists := q.dirty[key]; exists {
		// Already due; nothing to schedule.
		if _, busy := q.working[key]; !busy {
			return
		}
	}
	q.dirty[key] = struct{}{}
	if t, ok := q.timers[key]; ok {
		t.Stop()
	}
	q.timers[key] = time.AfterFunc(delay, func() {
		q.mu.Lock()
		defer q.mu.Unlock()
		delete(q.timers, key)
		if q.stopped {
			return
		}
		if _, busy := q.working[key]; busy {
			return // Done will requeue it
		}
		// Avoid double append if it is somehow already in the ready slice.
		for _, k := range q.q {
			if k == key {
				return
			}
		}
		q.q = append(q.q, key)
		q.cond.Signal()
	})
}

// Get blocks until a key is available. Returns ok=false after Shutdown and
// once the ready queue is empty.
func (q *queue) Get() (key string, ok bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for len(q.q) == 0 && !q.stopped {
		q.cond.Wait()
	}
	if len(q.q) == 0 {
		return "", false
	}
	key = q.q[0]
	q.q = q.q[1:]
	delete(q.dirty, key)
	q.working[key] = struct{}{}
	return key, true
}

// Done marks key as finished; if Add raced while it was in flight, the key is
// requeued for another pass.
func (q *queue) Done(key string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	delete(q.working, key)
	if _, wantAgain := q.dirty[key]; wantAgain && !q.stopped {
		q.q = append(q.q, key)
		q.cond.Signal()
	}
}

// Len returns the number of immediately ready keys (tests/metrics).
func (q *queue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.q)
}

// Shutdown stops accepting work and unblocks waiters. Pending timers are
// stopped; in-flight items are the caller's responsibility.
func (q *queue) Shutdown() {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.stopped {
		return
	}
	q.stopped = true
	for key, t := range q.timers {
		t.Stop()
		delete(q.timers, key)
	}
	q.cond.Broadcast()
}
