// Package decisionlog persists structured kernel decisions as JSON Lines.
//
// Every line is one DecisionRecord (schema cbcast.decision/v1) and carries the
// run id, per-node attempt id, incoming and delivered vector clocks, the
// waiting gaps and the rule that produced the verdict. Logs therefore tie a
// decision to its exact input and show the computation steps rather than only
// the outcome.
package decisionlog

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"

	"cbcast/internal/core"
)

// Multi fans records out to several sinks.
type Multi struct {
	mu    sync.Mutex
	sinks []core.DecisionLogger
}

// Add appends a sink.
func (m *Multi) Add(l core.DecisionLogger) {
	m.mu.Lock()
	m.sinks = append(m.sinks, l)
	m.mu.Unlock()
}

// LogDecision implements core.DecisionLogger.
func (m *Multi) LogDecision(rec core.DecisionRecord) {
	m.mu.Lock()
	sinks := append([]core.DecisionLogger(nil), m.sinks...)
	m.mu.Unlock()
	for _, s := range sinks {
		s.LogDecision(rec)
	}
}

// FileSink appends JSON Lines to a file.
type FileSink struct {
	mu sync.Mutex
	f  io.WriteCloser
}

// OpenFileSink creates/opens <dir>/<name>.jsonl for appending.
func OpenFileSink(dir, name string) (*FileSink, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, name+".jsonl")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	return &FileSink{f: f}, nil
}

// LogDecision implements core.DecisionLogger.
func (s *FileSink) LogDecision(rec core.DecisionRecord) {
	line, err := json.Marshal(rec)
	if err != nil {
		line = []byte(fmt.Sprintf(`{"schema":"cbcast.decision/v1","verdict":"log_error","reason":%q}`, err.Error()))
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, _ = s.f.Write(append(line, '\n'))
}

// Close flushes and closes the file.
func (s *FileSink) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.f.Close()
}

// MemorySink retains records in memory (tests use it to assert on evidence).
type MemorySink struct {
	mu      sync.Mutex
	Records []core.DecisionRecord
}

// LogDecision implements core.DecisionLogger.
func (s *MemorySink) LogDecision(rec core.DecisionRecord) {
	s.mu.Lock()
	s.Records = append(s.Records, rec)
	s.mu.Unlock()
}

// Snapshot returns a copy of all records so far.
func (s *MemorySink) Snapshot() []core.DecisionRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]core.DecisionRecord, len(s.Records))
	copy(out, s.Records)
	return out
}

// ByAttempt returns the record for an attempt id, if seen.
func (s *MemorySink) ByAttempt(attemptID string) (core.DecisionRecord, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, r := range s.Records {
		if r.AttemptID == attemptID {
			return r, true
		}
	}
	return core.DecisionRecord{}, false
}

// StampFile writes a small run manifest next to the log so archived logs can be
// correlated with configuration and timestamp.
func StampFile(dir, runID string, manifest any) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	name := filepath.Join(dir, fmt.Sprintf("manifest-%s-%s.json", runID, time.Now().UTC().Format("150405")))
	return os.WriteFile(name, raw, 0o644)
}
