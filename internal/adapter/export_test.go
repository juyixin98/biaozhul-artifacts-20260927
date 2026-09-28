package adapter

// Test-only accessors. Kept out of the production API.

func (s *Simulator) ProcessIDsForTest() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	ids := make([]string, 0, len(s.procs))
	for id := range s.procs {
		ids = append(ids, id)
	}
	return ids
}
