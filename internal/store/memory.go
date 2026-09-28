package store

import (
	"context"
	"fmt"
	"sort"
	"sync"

	"cbcast/internal/clock"
	"cbcast/internal/protocol"
)

type memRow struct {
	env      *protocol.Envelope
	status   Status
	deliver  uint64 // delivery sequence, 0 while pending
	received uint64 // receive order tie-breaker
}

// MemoryStore is an in-process Store guarded by one mutex.
type MemoryStore struct {
	mu       sync.Mutex
	rows     map[string]*memRow
	delClock protocol.VC
	seqHW    uint64
	recvCtr  uint64
	members  []string
}

// NewMemory creates a MemoryStore for the given membership.
func NewMemory(members []string) *MemoryStore {
	return &MemoryStore{
		rows:     map[string]*memRow{},
		delClock: clock.New(members),
		members:  append([]string(nil), members...),
	}
}

type memTxn struct {
	s *MemoryStore
}

func (s *MemoryStore) BeginDelivery(_ context.Context) (DeliveryTxn, error) {
	s.mu.Lock()
	return &memTxn{s: s}, nil
}

func (t *memTxn) CurrentClock() protocol.VC {
	return clock.Clone(t.s.delClock)
}

func (t *memTxn) CommitDelivery(ordered []*protocol.Envelope, newClock protocol.VC) error {
	defer t.s.mu.Unlock()
	for _, env := range ordered {
		row, ok := t.s.rows[env.MessageID]
		if !ok {
			return errInvariant("commit delivery: message %s not stored", env.MessageID)
		}
		if row.status == StatusDelivered {
			return errInvariant("commit delivery: message %s already delivered", env.MessageID)
		}
		t.s.seqHW++
		row.deliver = t.s.seqHW
		row.status = StatusDelivered
	}
	t.s.delClock = clock.Clone(newClock)
	return nil
}

func (t *memTxn) Discard() { t.s.mu.Unlock() }

func (s *MemoryStore) Get(_ context.Context, messageID string) (*protocol.Envelope, Status, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	row, ok := s.rows[messageID]
	if !ok {
		return nil, "", nil
	}
	return cloneEnv(row.env), row.status, nil
}

func (s *MemoryStore) Put(_ context.Context, env *protocol.Envelope) (PutOutcome, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if existing, ok := s.rows[env.MessageID]; ok {
		if existing.env.PayloadHash == env.PayloadHash {
			return PutDuplicate, nil
		}
		return PutConflict, nil
	}
	s.recvCtr++
	s.rows[env.MessageID] = &memRow{env: cloneEnv(env), status: StatusPending, received: s.recvCtr}
	return PutInserted, nil
}

func (s *MemoryStore) ListPending(_ context.Context) ([]*protocol.Envelope, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*protocol.Envelope
	for _, row := range s.rows {
		if row.status == StatusPending {
			out = append(out, cloneEnv(row.env))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		ri := s.rows[out[i].MessageID]
		rj := s.rows[out[j].MessageID]
		return ri.received < rj.received
	})
	return out, nil
}

func (s *MemoryStore) ListDeliveredSince(_ context.Context, sinceSeq uint64) ([]*protocol.Envelope, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	type item struct {
		seq uint64
		env *protocol.Envelope
	}
	var items []item
	for _, row := range s.rows {
		if row.status == StatusDelivered && row.deliver > sinceSeq {
			items = append(items, item{row.deliver, cloneEnv(row.env)})
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i].seq < items[j].seq })
	out := make([]*protocol.Envelope, len(items))
	for i, it := range items {
		out[i] = it.env
	}
	return out, nil
}

func (s *MemoryStore) State(_ context.Context) (State, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return State{DeliveredClock: clock.Clone(s.delClock), DeliverSeq: s.seqHW}, nil
}

func (s *MemoryStore) Close() error { return nil }

func cloneEnv(e *protocol.Envelope) *protocol.Envelope {
	cp := *e
	cp.Clock = clock.Clone(e.Clock)
	if e.Payload != nil {
		cp.Payload = append([]byte(nil), e.Payload...)
	}
	return &cp
}

type invariantError string

func (e invariantError) Error() string { return string(e) }

func errInvariant(format string, args ...any) error {
	return invariantError(fmt.Sprintf(format, args...))
}
