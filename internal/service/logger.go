package service

import (
	"encoding/json"
	"io"
	"os"
	"sync"
	"time"
)

// LogEntry is one structured, correlation-aware log line. Fields are explicit
// so the output schema is stable and reviewable in replay docs.
type LogEntry struct {
	Time            time.Time `json:"time"`
	Level           string    `json:"level"`
	RequestID       string    `json:"request_id,omitempty"`
	ClientRef       string    `json:"client_ref,omitempty"`
	Message         string    `json:"message"`
	Method          string    `json:"method,omitempty"`
	Path            string    `json:"path,omitempty"`
	Status          int       `json:"http_status,omitempty"`
	DurationMS      int64     `json:"duration_ms,omitempty"`
	ErrorCode       string    `json:"error_code,omitempty"`
	Error           string    `json:"error,omitempty"`
	Location        string    `json:"location,omitempty"`
	PrefixCount     int       `json:"prefix_count,omitempty"`
	TargetAddresses string    `json:"target_addresses,omitempty"`
	Equivalent      bool      `json:"exactly_equivalent,omitempty"`
	Warnings        []string  `json:"warnings,omitempty"`
	Steps           []string  `json:"steps,omitempty"`
	Version         string    `json:"version,omitempty"`
}

// Logger writes structured entries. Implementations must be safe for
// concurrent use.
type Logger interface {
	Log(LogEntry)
}

// JSONLogger emits one JSON object per line to a writer (stderr or a file),
// serialised by a mutex so concurrent requests never interleave bytes.
type JSONLogger struct {
	mu sync.Mutex
	w  io.Writer
}

// NewJSONLogger builds a logger writing to path. "-" or "" means stderr.
func NewJSONLogger(path string) (*JSONLogger, func() error, error) {
	var (
		f   io.WriteCloser
		err error
	)
	if path == "" || path == "-" {
		f = nopCloser{os.Stderr}
	} else {
		f, err = os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			return nil, nil, err
		}
	}
	l := &JSONLogger{w: f}
	return l, f.Close, nil
}

// Log implements Logger.
func (l *JSONLogger) Log(e LogEntry) {
	if e.Time.IsZero() {
		e.Time = time.Now().UTC()
	}
	b, err := json.Marshal(e)
	if err != nil {
		return
	}
	b = append(b, '\n')
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = l.w.Write(b)
}

type nopCloser struct{ io.Writer }

func (nopCloser) Close() error { return nil }
