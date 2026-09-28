package memstore

import (
	"context"
	"sync"
	"time"

	"natlab/internal/model"
)

// MemStore is an in-memory implementation of nat.StateStore used by the engine
// unit tests. It mirrors the SQLite semantics (active unique flow/port,
// time-based expiry) but contains no NAT logic: the decision rules live only
// in the engine under test.
type MemStore struct {
	mu       sync.Mutex
	runs     map[string]time.Time
	mappings []*model.Mapping
	events   []*model.Event
	nextID   int64
	// FailAt, when positive, forces the FailAt-th write call to error.
	failOn  map[string]bool
	callCnt int
}

// New builds an empty memory store.
func New() *MemStore { return NewMemStore() }

// NewMemStore builds an empty memory store.
func NewMemStore() *MemStore {
	return &MemStore{runs: map[string]time.Time{}, nextID: 1, failOn: map[string]bool{}}
}

// FailNextWrite makes the next mutating call return an error (compute-failure
// tests).
func (s *MemStore) FailNextWrite() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failOn["write"] = true
}

func (s *MemStore) fail() bool {
	if s.failOn["write"] {
		s.failOn["write"] = false
		return true
	}
	return false
}

// EnsureRun implements nat.StateStore.
func (s *MemStore) EnsureRun(_ context.Context, runID string) (time.Time, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if hw, ok := s.runs[runID]; ok {
		return hw, nil
	}
	s.runs[runID] = time.Time{}
	return time.Time{}, nil
}

// SetClock implements nat.StateStore.
func (s *MemStore) SetClock(_ context.Context, runID string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail() {
		return errInjected
	}
	s.runs[runID] = now
	return nil
}

// SweepExpired implements nat.StateStore.
func (s *MemStore) SweepExpired(_ context.Context, runID string, now time.Time) ([]*model.Mapping, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var closed []*model.Mapping
	for _, m := range s.mappings {
		if m.RunID == runID && model.IsActiveState(m.State) && !m.ExpiresAt.After(now) {
			m.State = model.StateClosed
			closed = append(closed, m)
		}
	}
	return closed, nil
}

// ActiveByFlow implements nat.StateStore.
func (s *MemStore) ActiveByFlow(_ context.Context, runID string, k model.FlowKey, now time.Time) (*model.Mapping, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, m := range s.mappings {
		if m.RunID != runID || !model.IsActiveState(m.State) || m.ExpiresAt.Equal(now) || m.ExpiresAt.Before(now) {
			continue
		}
		if m.Protocol == k.Protocol && m.SrcIP == k.SrcIP && m.SrcPort == k.SrcPort &&
			m.DstIP == k.DstIP && m.DstPort == k.DstPort {
			return clone(m), nil
		}
	}
	return nil, nil
}

// ActiveByExtPort implements nat.StateStore.
func (s *MemStore) ActiveByExtPort(_ context.Context, runID string, proto model.Protocol, port uint16, now time.Time) (*model.Mapping, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, m := range s.mappings {
		if m.RunID == runID && m.Protocol == proto && m.MappedPort == port &&
			model.IsActiveState(m.State) && m.ExpiresAt.After(now) {
			return clone(m), nil
		}
	}
	return nil, nil
}

// HistoryByExtPort implements nat.StateStore.
func (s *MemStore) HistoryByExtPort(_ context.Context, runID string, proto model.Protocol, port uint16) (*model.Mapping, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var best *model.Mapping
	for _, m := range s.mappings {
		if m.RunID == runID && m.Protocol == proto && m.MappedPort == port {
			if best == nil || m.ID > best.ID {
				best = m
			}
		}
	}
	if best == nil {
		return nil, nil
	}
	return clone(best), nil
}

// InsertMapping implements nat.StateStore.
func (s *MemStore) InsertMapping(_ context.Context, runID string, m *model.Mapping) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail() {
		return 0, errInjected
	}
	for _, e := range s.mappings {
		if e.RunID == runID && model.IsActiveState(e.State) &&
			(e.MappedPort == m.MappedPort && e.Protocol == m.Protocol) {
			return 0, errDup
		}
	}
	m.ID = s.nextID
	s.nextID++
	m.RunID = runID
	s.mappings = append(s.mappings, clone(m))
	return m.ID, nil
}

// UpdateMapping implements nat.StateStore.
func (s *MemStore) UpdateMapping(_ context.Context, runID string, m *model.Mapping) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail() {
		return errInjected
	}
	for _, e := range s.mappings {
		if e.ID == m.ID && e.RunID == runID {
			e.State = m.State
			e.LastUsedAt = m.LastUsedAt
			e.ExpiresAt = m.ExpiresAt
			return nil
		}
	}
	return errNotFound
}

// CloseMapping implements nat.StateStore.
func (s *MemStore) CloseMapping(_ context.Context, runID string, id int64, now time.Time) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, m := range s.mappings {
		if m.ID == id && m.RunID == runID && model.IsActiveState(m.State) {
			m.State = model.StateClosed
			m.LastUsedAt = now
			return true, nil
		}
	}
	return false, nil
}

// CountActive implements nat.StateStore.
func (s *MemStore) CountActive(_ context.Context, runID string, now time.Time) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, m := range s.mappings {
		if m.RunID == runID && model.IsActiveState(m.State) && m.ExpiresAt.After(now) {
			n++
		}
	}
	return n, nil
}

// ListMappings implements nat.StateStore.
func (s *MemStore) ListMappings(_ context.Context, runID string, activeOnly bool) ([]*model.Mapping, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*model.Mapping
	for _, m := range s.mappings {
		if m.RunID != runID {
			continue
		}
		if activeOnly && !model.IsActiveState(m.State) {
			continue
		}
		out = append(out, clone(m))
	}
	return out, nil
}

// AppendEvent implements nat.StateStore.
func (s *MemStore) AppendEvent(_ context.Context, e *model.Event) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail() {
		return errInjected
	}
	cp := *e
	cp.ID = int64(len(s.events) + 1)
	s.events = append(s.events, &cp)
	e.ID = cp.ID
	return nil
}

// ListEvents implements nat.StateStore.
func (s *MemStore) ListEvents(_ context.Context, runID string, limit int) ([]*model.Event, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*model.Event
	for _, e := range s.events {
		if e.RunID == runID {
			out = append(out, cloneEvent(e))
		}
	}
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

// Events returns all stored events (test helper).
func (s *MemStore) Events() []*model.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*model.Event, len(s.events))
	for i, e := range s.events {
		out[i] = cloneEvent(e)
	}
	return out
}

func clone(m *model.Mapping) *model.Mapping {
	cp := *m
	return &cp
}

func cloneEvent(e *model.Event) *model.Event {
	cp := *e
	return &cp
}
