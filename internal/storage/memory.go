package storage

import (
	"context"
	"sort"
	"sync"
	"time"

	"natlab/internal/model"
)

// Memory is the deterministic, zero-dependency Store used by unit tests and by
// runs configured without a SQLite path.
type Memory struct {
	mu sync.Mutex

	runs       map[string]RunInfo
	mappings   map[string]map[string]StoredMapping // runID -> id -> mapping
	tombs      []StoredTombstone
	decisions  map[string][]model.Decision // runID -> ordered decisions
	watermarks map[string]time.Time
}

// NewMemory creates an empty in-memory store.
func NewMemory() *Memory {
	return &Memory{
		runs:       map[string]RunInfo{},
		mappings:   map[string]map[string]StoredMapping{},
		decisions:  map[string][]model.Decision{},
		watermarks: map[string]time.Time{},
	}
}

func (s *Memory) UpsertRun(_ context.Context, r RunInfo) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.runs[r.ID]; !ok {
		s.runs[r.ID] = r
		if s.mappings[r.ID] == nil {
			s.mappings[r.ID] = map[string]StoredMapping{}
		}
		if _, ok := s.watermarks[r.ID]; !ok {
			s.watermarks[r.ID] = r.Watermark
		}
	}
	return nil
}

func (s *Memory) GetRun(_ context.Context, id string) (RunInfo, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.runs[id]
	if !ok {
		return RunInfo{}, &ErrNotFound{What: "run " + id}
	}
	r.Watermark = s.watermarks[id]
	return r, nil
}

func (s *Memory) PutMapping(_ context.Context, m StoredMapping) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.mappings[m.RunID] == nil {
		s.mappings[m.RunID] = map[string]StoredMapping{}
	}
	s.mappings[m.RunID][m.ID] = m
	return nil
}

func (s *Memory) UpdateMapping(_ context.Context, m StoredMapping) error {
	return s.PutMapping(context.Background(), m)
}

func (s *Memory) DeleteMapping(_ context.Context, runID, id string, _ time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if set := s.mappings[runID]; set != nil {
		delete(set, id)
	}
	return nil
}

func (s *Memory) ListMappings(_ context.Context, runID string) ([]StoredMapping, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []StoredMapping
	for _, m := range s.mappings[runID] {
		out = append(out, m)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].ExternalPort != out[j].ExternalPort {
			return out[i].ExternalPort < out[j].ExternalPort
		}
		return out[i].ID < out[j].ID
	})
	return out, nil
}

func (s *Memory) AddTombstone(_ context.Context, t StoredTombstone) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.tombs = append(s.tombs, t)
	return nil
}

func (s *Memory) FindTombstone(_ context.Context, runID string, proto model.Protocol,
	extPort uint16, remoteIP string, remotePort uint16, at time.Time) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, t := range s.tombs {
		if t.RunID != runID || t.Proto != proto || t.ExternalPort != extPort {
			continue
		}
		if at.After(t.RetainUntil) {
			continue
		}
		if t.RemoteIP == remoteIP && t.RemotePort == remotePort {
			return true, nil
		}
	}
	return false, nil
}

func (s *Memory) PruneTombstones(_ context.Context, runID string, at time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	kept := s.tombs[:0]
	for _, t := range s.tombs {
		if t.RunID == runID && at.After(t.RetainUntil) {
			continue
		}
		kept = append(kept, t)
	}
	s.tombs = kept
	return nil
}

func (s *Memory) AppendDecision(_ context.Context, d model.Decision) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.decisions[d.RunID] = append(s.decisions[d.RunID], d)
	return nil
}

func (s *Memory) ListDecisions(_ context.Context, runID string, fromSeq int64, limit int) ([]model.Decision, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []model.Decision
	for _, d := range s.decisions[runID] {
		if d.Seq < fromSeq {
			continue
		}
		out = append(out, d)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (s *Memory) SetWatermark(_ context.Context, runID string, at time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if cur := s.watermarks[runID]; at.After(cur) {
		s.watermarks[runID] = at
	}
	return nil
}

// Close releases nothing for the memory store.
func (s *Memory) Close() error { return nil }

// Watermark is a test helper to read the monotonic clock.
func (s *Memory) Watermark(runID string) time.Time {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.watermarks[runID]
}
