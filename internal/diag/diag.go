// Package diag provides replayable apply diagnostics: every attempt gets a
// run id, and the journal records the inputs, intermediate merge state and the
// decision reason. Entries are emitted as single-line JSON so they survive
// log shipping and can be fed back to the test suite.
package diag

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

// Event is one journal record. Fields are pointers/omitempty so a failed
// attempt and a successful one share the same envelope.
type Event struct {
	RunID      string          `json:"run_id"`
	At         string          `json:"at"`
	Phase      string          `json:"phase"` // received | merged | conflict | committed | error
	ResourceID string          `json:"resource_id"`
	Manager    string          `json:"manager,omitempty"`
	Revision   *int64          `json:"revision,omitempty"`
	Reason     string          `json:"reason,omitempty"`
	Category   string          `json:"category,omitempty"`
	Code       string          `json:"code,omitempty"`
	Force      bool            `json:"force,omitempty"`
	Summary    string          `json:"summary,omitempty"`
	Detail     json.RawMessage `json:"detail,omitempty"`
}

// Logger is the diagnostics sink.
type Logger struct {
	mu  sync.Mutex
	w   io.Writer
	enc *json.Encoder
	f   *os.File
}

// NewLogger writes journal lines to w (nil disables logging).
func NewLogger(w io.Writer) *Logger {
	l := &Logger{w: w}
	if w != nil {
		l.enc = json.NewEncoder(w)
	}
	return l
}

// OpenFile creates/opens a JSON-lines journal file.
func OpenFile(path string) (*Logger, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	return &Logger{w: f, enc: json.NewEncoder(f), f: f}, nil
}

func (l *Logger) Log(ev Event) {
	if l == nil || l.enc == nil {
		return
	}
	if ev.At == "" {
		ev.At = time.Now().UTC().Format(time.RFC3339Nano)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	_ = l.enc.Encode(ev)
}

func (l *Logger) Close() error {
	if l == nil || l.f == nil {
		return nil
	}
	return l.f.Close()
}

// NewRunID returns a time-prefixed random id such as
// "r-20260928T101543Z-9f3a2b7c01d4e6f8". The timestamp prefix keeps runs
// sortable on disk; the random suffix disambiguates same-second attempts.
func NewRunID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("r-%d", time.Now().UnixNano())
	}
	return "r-" + time.Now().UTC().Format("20060102T150405Z") + "-" + hex.EncodeToString(b[:])
}
