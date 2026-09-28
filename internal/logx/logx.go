// Package logx provides structured JSON logging with run correlation.
//
// Every log line carries:
//   - ts:        UTC timestamp
//   - level:     debug | info | error
//   - version:   build version string
//   - run_id:    the placement/reconcile run this line belongs to
//   - component: scheduler | reconcile | http | store
//
// plus event-specific fields.
//
// Unknown/error conditions are logged at error level and never folded into
// a success message.
package logx

import (
	"encoding/json"
	"io"
	"os"
	"sync"
	"time"

	"placer/internal/version"
)

// Level controls verbosity.
type Level int

const (
	LevelDebug Level = iota
	LevelInfo
	LevelError
)

// ParseLevel parses debug|info|error.
func ParseLevel(s string) (Level, bool) {
	switch s {
	case "debug":
		return LevelDebug, true
	case "info", "":
		return LevelInfo, true
	case "error":
		return LevelError, true
	}
	return LevelInfo, false
}

// Logger is a leveled structured logger safe for concurrent use.
type Logger struct {
	mu        sync.Mutex
	w         io.Writer
	level     Level
	component string
	runID     string
}

// New creates a logger writing to w.
func New(w io.Writer, level Level) *Logger {
	if w == nil {
		w = os.Stderr
	}
	return &Logger{w: w, level: level}
}

// With returns a child logger annotating every line with component and/or
// runID. Empty arguments leave the parent value unchanged. The child shares
// the parent's writer and mutex (it must not copy the mutex).
func (l *Logger) With(component, runID string) *Logger {
	c := &Logger{w: l.w, level: l.level, component: l.component, runID: l.runID}
	if component != "" {
		c.component = component
	}
	if runID != "" {
		c.runID = runID
	}
	return c
}

// WithRun is shorthand for With("", runID).
func (l *Logger) WithRun(runID string) *Logger { return l.With("", runID) }

func (l *Logger) emit(level Level, event string, fields map[string]any) {
	if level < l.level {
		return
	}
	rec := map[string]any{
		"ts":      time.Now().UTC().Format(time.RFC3339Nano),
		"level":   levelName(level),
		"version": version.String(),
		"event":   event,
	}
	if l.component != "" {
		rec["component"] = l.component
	}
	if l.runID != "" {
		rec["run_id"] = l.runID
	}
	for k, v := range fields {
		rec[k] = v
	}
	b, err := json.Marshal(rec)
	if err != nil {
		b = []byte(`{"level":"error","event":"log_marshal_failed"}`)
	}
	b = append(b, '\n')
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = l.w.Write(b)
}

func levelName(l Level) string {
	switch l {
	case LevelDebug:
		return "debug"
	case LevelError:
		return "error"
	default:
		return "info"
	}
}

// Debug logs at debug level (search progress, per-node scores).
func (l *Logger) Debug(event string, fields map[string]any) { l.emit(LevelDebug, event, fields) }

// Info logs at info level (run start/end, decisions).
func (l *Logger) Info(event string, fields map[string]any) { l.emit(LevelInfo, event, fields) }

// Error logs at error level. err is included as "err".
func (l *Logger) Error(event string, err error, fields map[string]any) {
	if fields == nil {
		fields = map[string]any{}
	}
	if err != nil {
		fields["err"] = err.Error()
	}
	l.emit(LevelError, event, fields)
}
