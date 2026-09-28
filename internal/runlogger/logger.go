// Package runlogger writes replay-grade per-run logs. Every run has an ID;
// every entry carries the intermediate object hash, the patch and the reason
// for the judgement, so a failing scenario can be reconstructed from the log
// alone (see testdata/fixtures and docs for the recorded sample).
package runlogger

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"admission/internal/admission"
)

// Entry is the serialized form of admission.LogEntry.
type Entry struct {
	Record     string `json:"record"` // always "run-log/v1"
	RunID      string `json:"runId"`
	Time       string `json:"time"`
	Level      string `json:"level"`
	Phase      string `json:"phase,omitempty"`
	Pass       int    `json:"pass,omitempty"`
	Plugin     string `json:"plugin,omitempty"`
	Category   string `json:"category,omitempty"`
	BeforeHash string `json:"beforeHash,omitempty"`
	AfterHash  string `json:"afterHash,omitempty"`
	Message    string `json:"message"`
	Patch      any    `json:"patch,omitempty"`
}

// Logger writes one JSON object per line (JSONL). A nil directory disables
// file output; entries are still captured in memory for assertions.
type Logger struct {
	mu      sync.Mutex
	dir     string
	buf     []admission.LogEntry
	now     func() time.Time
	written map[string]bool
}

// New creates a logger rooted at dir (created lazily).
func New(dir string) *Logger {
	return &Logger{dir: dir, now: time.Now, written: map[string]bool{}}
}

// Log implements admission.RunLogger.
func (l *Logger) Log(e admission.LogEntry) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.buf = append(l.buf, e)
	if l.dir == "" {
		return
	}
	if err := os.MkdirAll(l.dir, 0o755); err != nil {
		return
	}
	path := filepath.Join(l.dir, "run-"+safe(e.RunID)+".jsonl")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	en := Entry{
		Record: "run-log/v1", RunID: e.RunID, Time: e.Time.UTC().Format(time.RFC3339Nano),
		Level: e.Level, Phase: string(e.Phase), Pass: e.Pass, Plugin: e.Plugin,
		Category: string(e.Category), BeforeHash: e.BeforeHash, AfterHash: e.AfterHash,
		Message: e.Message,
	}
	if len(e.Patch) > 0 {
		en.Patch = e.Patch
	}
	b, _ := json.Marshal(en)
	_, _ = f.Write(append(b, '\n'))
}

// Entries returns a copy of all entries captured this process, optionally
// restricted to one run.
func (l *Logger) Entries(runID string) []admission.LogEntry {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]admission.LogEntry, 0, len(l.buf))
	for _, e := range l.buf {
		if runID == "" || e.RunID == runID {
			out = append(out, e)
		}
	}
	return out
}

// RunIDs lists the distinct run IDs seen, in first-seen order.
func (l *Logger) RunIDs() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	seen := map[string]bool{}
	var ids []string
	for _, e := range l.buf {
		if !seen[e.RunID] {
			seen[e.RunID] = true
			ids = append(ids, e.RunID)
		}
	}
	sort.Strings(ids)
	return ids
}

func safe(s string) string {
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '-', c == '_':
			out = append(out, c)
		default:
			out = append(out, '_')
		}
	}
	return string(out)
}
