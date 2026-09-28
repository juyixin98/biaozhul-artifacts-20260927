// Package storemem is the in-memory Store implementation.
//
// It is fully deterministic when driven with an injected clock from tests and
// applies the same pure kernel rules inside one mutex-held critical section
// that storepg applies inside one SQL transaction.
package storemem

import (
	"context"
	"sort"
	"sync"
	"time"

	"localbroker/internal/kernel"
	"localbroker/internal/protocol"
	"localbroker/internal/store"
)

// Store is an in-memory, goroutine-safe Store.
type Store struct {
	mu     sync.Mutex
	queues map[string]protocol.QueueConfig
	msgs   map[string]map[string]*protocol.Message
	events map[string][]kernel.Event
	seq    int64
	closed bool
}

// New returns an empty in-memory store.
func New() *Store {
	return &Store{
		queues: map[string]protocol.QueueConfig{},
		msgs:   map[string]map[string]*protocol.Message{},
		events: map[string][]kernel.Event{},
	}
}

func (s *Store) checkOpen() error {
	if s.closed {
		return store.ErrClosed
	}
	return nil
}

func (s *Store) appendEvent(queue string, ev kernel.Event, runID string) kernel.Event {
	s.seq++
	ev.Seq = s.seq
	ev.RunID = runID
	s.events[queue] = append(s.events[queue], ev)
	return ev
}

// CreateQueue registers a queue.
func (s *Store) CreateQueue(_ context.Context, cfg protocol.QueueConfig) error {
	if err := s.checkOpen(); err != nil {
		return err
	}
	if cfg.Name == "" {
		return &kernel.InvalidArgument{Msg: "queue name must not be empty"}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.queues[cfg.Name]; ok {
		return kernel.ErrQueueExists
	}
	s.queues[cfg.Name] = cfg
	s.msgs[cfg.Name] = map[string]*protocol.Message{}
	return nil
}

// QueueConfig returns a queue's config.
func (s *Store) QueueConfig(_ context.Context, name string) (protocol.QueueConfig, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	cfg, ok := s.queues[name]
	if !ok {
		return protocol.QueueConfig{}, kernel.ErrQueueNotFound
	}
	return cfg, nil
}

// Queues lists all configured queues sorted by name.
func (s *Store) Queues(_ context.Context) ([]protocol.QueueConfig, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	names := make([]string, 0, len(s.queues))
	for name := range s.queues {
		names = append(names, name)
	}
	sort.Strings(names)
	out := make([]protocol.QueueConfig, 0, len(names))
	for _, name := range names {
		out = append(out, s.queues[name])
	}
	return out, nil
}

// Publish stores a message and its enqueued event atomically.
func (s *Store) Publish(_ context.Context, queue, id string, body []byte, now time.Time, runID string) error {
	if err := s.checkOpen(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.queues[queue]; !ok {
		return kernel.ErrQueueNotFound
	}
	if _, dup := s.msgs[queue][id]; dup {
		return &kernel.InvalidArgument{Msg: "duplicate message id " + id}
	}
	m := &protocol.Message{
		ID:         id,
		Queue:      queue,
		Body:       append([]byte(nil), body...),
		State:      protocol.StateAvailable,
		EnqueuedAt: now,
		UpdatedAt:  now,
		Failures:   []protocol.Failure{},
	}
	s.msgs[queue][id] = m
	s.appendEvent(queue, kernel.NewEnqueued(id, now), runID)
	return nil
}

func leaseView(m *protocol.Message) protocol.LeaseView {
	return protocol.LeaseView{
		ID:          m.ID,
		State:       m.State,
		Attempts:    m.Attempts,
		Receipt:     m.Receipt,
		ReceiptGen: m.ReceiptGen,
		LastReceipt: m.LastReceipt,
		Deadline:    m.Deadline,
	}
}

// sortedIDs returns message ids in FIFO (id-sortable) order.
func sortedIDs(byID map[string]*protocol.Message) []string {
	ids := make([]string, 0, len(byID))
	for id := range byID {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// Claim grants one delivery atomically.
func (s *Store) Claim(_ context.Context, queue string, now time.Time, receipt, runID string) (store.ClaimResult, error) {
	if err := s.checkOpen(); err != nil {
		return store.ClaimResult{}, err
	}
	if receipt == "" {
		return store.ClaimResult{}, &kernel.InvalidArgument{Msg: "receipt must not be empty"}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	cfg, ok := s.queues[queue]
	if !ok {
		return store.ClaimResult{}, kernel.ErrQueueNotFound
	}
	byID := s.msgs[queue]
	var picked *protocol.Message
	for _, id := range sortedIDs(byID) {
		if byID[id].State == protocol.StateAvailable {
			picked = byID[id]
			break
		}
	}
	if picked == nil {
		return store.ClaimResult{}, kernel.ErrNoMessage
	}

	prev := leaseView(picked)
	if !kernel.DecideClaim(prev.Attempts, cfg.MaxAttempts) {
		// Defensive: an available row must always have budget (expiry
		// dead-letters the exhausted attempt). Never grant a delivery the
		// policy forbids; dead-letter with an explicit reason instead.
		o := kernel.ExpireOutcome{Dead: true, ToState: protocol.StateDead,
			Failure: &protocol.Failure{Attempt: prev.Attempts, Kind: protocol.FailLeaseExpired,
				Reason: "claim blocked: delivery budget already exhausted", At: now}}
		ev := kernel.NewExpireEvent(picked.ID, prev, o, now)
		s.applyExpireLike(picked, queue, ev, now, runID)
		return store.ClaimResult{}, kernel.ErrNoMessage
	}

	ev, cl := kernel.NewClaimed(picked.ID, prev, cfg.VisibilityTimeout, now, receipt)
	s.appendEvent(queue, ev, runID)

	picked.LastReceipt = prev.Receipt
	picked.Receipt = cl.Receipt
	picked.ReceiptGen = cl.ReceiptGen
	picked.State = protocol.StateInvisible
	picked.Attempts = cl.Attempts
	picked.Deadline = cl.Deadline
	picked.UpdatedAt = now

	return store.ClaimResult{
		MessageID: picked.ID,
		Body:      append([]byte(nil), picked.Body...),
		Attempts:  cl.Attempts,
		Claimed:   cl,
	}, nil
}

// findByReceipt locates the row a receipt addresses: a current live receipt
// wins; otherwise the row that last issued it (for precise stale/expired
// errors); nil when the receipt is unknown.
func (s *Store) findByReceipt(queue, receipt string) *protocol.Message {
	byID := s.msgs[queue]
	var lastOwner *protocol.Message
	for _, id := range sortedIDs(byID) {
		m := byID[id]
		if m.Receipt == receipt {
			return m
		}
		if lastOwner == nil && m.LastReceipt == receipt {
			lastOwner = m
		}
	}
	return lastOwner
}

// Extend validates and extends a lease atomically.
func (s *Store) Extend(_ context.Context, queue, receipt string, extra time.Duration, now time.Time, runID string) (time.Time, error) {
	if err := s.checkOpen(); err != nil {
		return time.Time{}, err
	}
	if extra <= 0 {
		return time.Time{}, &kernel.InvalidArgument{Msg: "extend duration must be > 0"}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.queues[queue]; !ok {
		return time.Time{}, kernel.ErrQueueNotFound
	}
	m := s.findByReceipt(queue, receipt)
	if m == nil {
		return time.Time{}, kernel.ErrInvalidReceipt
	}
	cur := leaseView(m)
	if err := kernel.CheckReceipt(cur, receipt, now); err != nil {
		return time.Time{}, err
	}
	newDeadline := now.Add(extra)
	ev := kernel.NewExtended(m.ID, receipt, cur.ReceiptGen, newDeadline, extra, now)
	s.appendEvent(queue, ev, runID)
	m.Deadline = newDeadline
	m.UpdatedAt = now
	return newDeadline, nil
}

// Ack confirms a live receipt.
func (s *Store) Ack(_ context.Context, queue, receipt string, now time.Time, runID string) error {
	if err := s.checkOpen(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.queues[queue]; !ok {
		return kernel.ErrQueueNotFound
	}
	m := s.findByReceipt(queue, receipt)
	if m == nil {
		return kernel.ErrInvalidReceipt
	}
	if err := kernel.CheckReceipt(leaseView(m), receipt, now); err != nil {
		return err
	}
	ev := kernel.NewAcked(m.ID, receipt, m.ReceiptGen, now)
	s.appendEvent(queue, ev, runID)
	m.State = protocol.StateAcked
	m.Receipt = ""
	m.Deadline = time.Time{}
	m.UpdatedAt = now
	return nil
}

// Nack reports explicit failure of a live receipt.
func (s *Store) Nack(_ context.Context, queue, receipt, reason string, now time.Time, runID string) error {
	if err := s.checkOpen(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	cfg, ok := s.queues[queue]
	if !ok {
		return kernel.ErrQueueNotFound
	}
	m := s.findByReceipt(queue, receipt)
	if m == nil {
		return kernel.ErrInvalidReceipt
	}
	cur := leaseView(m)
	if err := kernel.CheckReceipt(cur, receipt, now); err != nil {
		return err
	}
	o := kernel.DecideNack(cur, cfg.MaxAttempts, reason, now)
	ev := kernel.NewNackEvent(m.ID, cur, o, now)
	s.applyExpireLike(m, queue, ev, now, runID)
	return nil
}

// applyExpireLike persists an expire/nack/dead-letter event already stamped
// with RunID by the caller path; here we stamp it uniformly and mutate the row.
func (s *Store) applyExpireLike(m *protocol.Message, queue string, ev kernel.Event, now time.Time, runID string) {
	ev.RunID = runID
	s.appendEvent(queue, ev, runID)
	m.State = ev.ToState
	if ev.Failure != nil {
		m.Failures = append(m.Failures, *ev.Failure)
	}
	m.LastReceipt = m.Receipt
	m.Receipt = ev.Receipt // "" -> lease released
	if ev.ToState == protocol.StateDead || ev.ToState == protocol.StateAvailable {
		m.Deadline = time.Time{}
	}
	m.UpdatedAt = now
}

// ExpireDue reclaims every due lease atomically (one critical section = one
// transaction boundary).
func (s *Store) ExpireDue(_ context.Context, queue string, now time.Time, runID string) (int, error) {
	if err := s.checkOpen(); err != nil {
		return 0, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	cfg, ok := s.queues[queue]
	if !ok {
		return 0, kernel.ErrQueueNotFound
	}
	n := 0
	for _, id := range sortedIDs(s.msgs[queue]) {
		m := s.msgs[queue][id]
		if m.State != protocol.StateInvisible {
			continue
		}
		if m.Deadline.After(now) {
			continue
		}
		cur := leaseView(m)
		o := kernel.DecideExpire(cur, cfg.MaxAttempts, now)
		ev := kernel.NewExpireEvent(m.ID, cur, o, now)
		s.applyExpireLike(m, queue, ev, now, runID)
		n++
	}
	return n, nil
}

func clone(m *protocol.Message) protocol.Message {
	c := *m
	c.Body = append([]byte(nil), m.Body...)
	c.Failures = append([]protocol.Failure(nil), m.Failures...)
	return c
}

// Message returns a copy of live state.
func (s *Store) Message(_ context.Context, queue, id string) (protocol.Message, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.queues[queue]; !ok {
		return protocol.Message{}, kernel.ErrQueueNotFound
	}
	m, ok := s.msgs[queue][id]
	if !ok {
		return protocol.Message{}, kernel.ErrNoMessage
	}
	return clone(m), nil
}

// ListDead returns dead-lettered messages oldest first, bodies and full
// failure reasons retained.
func (s *Store) ListDead(_ context.Context, queue string) ([]protocol.Message, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.queues[queue]; !ok {
		return nil, kernel.ErrQueueNotFound
	}
	var out []protocol.Message
	for _, id := range sortedIDs(s.msgs[queue]) {
		m := s.msgs[queue][id]
		if m.State == protocol.StateDead {
			out = append(out, clone(m))
		}
	}
	return out, nil
}

// ListMessages returns all rows oldest first.
func (s *Store) ListMessages(_ context.Context, queue string) ([]protocol.Message, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.queues[queue]; !ok {
		return nil, kernel.ErrQueueNotFound
	}
	var out []protocol.Message
	for _, id := range sortedIDs(s.msgs[queue]) {
		out = append(out, clone(s.msgs[queue][id]))
	}
	return out, nil
}

// Events returns a copy of the ordered event log.
func (s *Store) Events(_ context.Context, queue string) ([]kernel.Event, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.queues[queue]; !ok {
		return nil, kernel.ErrQueueNotFound
	}
	return append([]kernel.Event(nil), s.events[queue]...), nil
}

// Close marks the store closed.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closed = true
	return nil
}
