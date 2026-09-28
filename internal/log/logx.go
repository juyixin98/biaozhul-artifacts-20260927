// Package logx provides minimal structured (JSON-line) logging with a
// correlation RunID on every entry. The same writer is shared by the server,
// merge engine diagnostics and tests so a single log file can replay a run.
package logx

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

type Logger struct {
	mu     sync.Mutex
	w      io.Writer
	runID  string
	fields map[string]any
}

// New creates a logger. If w is nil logs go to stderr.
func New(w io.Writer, runID string) *Logger {
	if w == nil {
		w = os.Stderr
	}
	return &Logger{w: w, runID: runID, fields: map[string]any{}}
}

// With returns a child logger carrying extra fields on every emitted entry.
func (l *Logger) With(fields map[string]any) *Logger {
	merged := make(map[string]any, len(l.fields)+len(fields))
	for k, v := range l.fields {
		merged[k] = v
	}
	for k, v := range fields {
		merged[k] = v
	}
	return &Logger{w: l.w, runID: l.runID, fields: merged}
}

func (l *Logger) RunID() string { return l.runID }

// Log emits one structured record at the given level.
func (l *Logger) Log(level, event string, fields map[string]any) {
	rec := make(map[string]any, len(l.fields)+len(fields)+4)
	for k, v := range l.fields {
		rec[k] = v
	}
	for k, v := range fields {
		rec[k] = v
	}
	rec["ts"] = time.Now().UTC().Format(time.RFC3339Nano)
	rec["level"] = level
	rec["event"] = event
	rec["run_id"] = l.runID
	b, err := json.Marshal(rec)
	if err != nil {
		b = []byte(`{"level":"error","event":"log_marshal_failed"}`)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = io.WriteString(l.w, string(b)+"\n")
}

func (l *Logger) Info(event string, fields map[string]any)  { l.Log("info", event, fields) }
func (l *Logger) Warn(event string, fields map[string]any)  { l.Log("warn", event, fields) }
func (l *Logger) Error(event string, fields map[string]any) { l.Log("error", event, fields) }
func (l *Logger) Debug(event string, fields map[string]any) { l.Log("debug", event, fields) }

// MultiWriter fans logs out to several writers (e.g. stderr + a replay file).
func MultiWriter(ws ...io.Writer) io.Writer { return &multi{ws: ws} }

type multi struct{ ws []io.Writer }

func (m *multi) Write(p []byte) (int, error) {
	var firstErr error
	for _, w := range m.ws {
		n, err := w.Write(p)
		if err != nil && firstErr == nil {
			firstErr = err
		}
		if n != len(p) && firstErr == nil {
			firstErr = io.ErrShortWrite
		}
	}
	if firstErr != nil {
		return 0, firstErr
	}
	return len(p), nil
}

var _ fmt.Stringer = (*Logger)(nil)

func (l *Logger) String() string { return "logger(run=" + l.runID + ")" }
