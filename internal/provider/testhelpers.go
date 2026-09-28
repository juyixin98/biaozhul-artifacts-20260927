package provider

import "infraplanner/internal/model"

// Test-only state manipulation hooks. These are intentionally in a *_test.go
// adjacent helper so production code cannot mutate the simulated world
// outside the Provider contract; they are used by higher-package tests that
// drive the in-process fake like an external actor would.

// MuLock acquires the simulated control-plane lock.
func (s *Sim) MuLock() { s.mu.Lock() }

// MuUnlock releases the simulated control-plane lock.
func (s *Sim) MuUnlock() { s.mu.Unlock() }

// SetLiveForTest overwrites (or inserts) a live resource by its physical id.
func (s *Sim) SetLiveForTest(l model.Live) {
	s.mem.mu.Lock()
	defer s.mem.mu.Unlock()
	cp := l
	s.mem.byID[l.ID] = &cp
	s.mem.nameIndex[l.Key] = l.ID
}

// DeleteForTest removes a resource by logical key, regardless of id.
func (s *Sim) DeleteForTest(k model.Key) {
	s.mem.mu.Lock()
	defer s.mem.mu.Unlock()
	id, ok := s.mem.nameIndex[k]
	if !ok {
		return
	}
	delete(s.mem.byID, id)
	delete(s.mem.nameIndex, k)
}
