// Package runlog writes one JSON line per admission attempt to a local file:
// run id, request, final verdict, per-step intermediate states and the
// reasoning category. These lines are the replay artifact the task asks tests
// to preserve — a failing scenario can be reconstructed by feeding the stored
// request back in and comparing the step-by-step patches.
package runlog

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"admission/internal/model"
)

// Line is one append-only record.
type Line struct {
	RunID        string         `json:"runId"`
	Time         time.Time      `json:"time"`
	Request      model.Request  `json:"request"`
	Decision     model.Decision `json:"decision"`
	Reason       model.Reason   `json:"reason,omitempty"`
	Category     string         `json:"category"`
	Message      string         `json:"message,omitempty"`
	Steps        []model.Step   `json:"steps"`
	FinalSummary *model.Summary `json:"finalSummary,omitempty"`
}

// Logger serializes appends to one .jsonl file. A nil *Logger is a valid
// no-op (used when no log directory is configured).
type Logger struct {
	mu sync.Mutex
	f  *os.File
}

// Open creates/opens <dir>/admission-runs.jsonl.
func Open(dir string) (*Logger, error) {
	if dir == "" {
		return nil, nil
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, "admission-runs.jsonl"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &Logger{f: f}, nil
}

// Append writes one line.
func (l *Logger) Append(line Line) error {
	if l == nil {
		return nil
	}
	b, err := json.Marshal(line)
	if err != nil {
		return err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, err := l.f.Write(append(b, '\n')); err != nil {
		return fmt.Errorf("write run log: %w", err)
	}
	return nil
}

// Close flushes the file.
func (l *Logger) Close() error {
	if l == nil {
		return nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.f.Close()
}
