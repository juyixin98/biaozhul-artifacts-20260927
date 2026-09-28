package core

import (
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

// Logger is the minimal structured-logging surface the engine depends on.
// Fields are key/value pairs, never formatted into the message by the caller,
// so test logs stay machine-greppable. Decision records always carry the
// fields "run_id", "node", "vc", "reason" (for non-deliveries) and either
// "delivered_id" or "env_id", tying every judgement back to its input packet.
type Logger interface {
	Info(msg string, fields ...Field)
	Warn(msg string, fields ...Field)
	Error(msg string, fields ...Field)
	With(fields ...Field) Logger
}

// Field is one structured log field.
type Field struct {
	K string
	V any
}

// F is shorthand for a log field.
func F(k string, v any) Field { return Field{K: k, V: v} }

// textLogger emits line-oriented key=value records. JSON would hide the
// decision basis in test output; key=value keeps every run greppable while a
// parallel machine-readable copy is written by the test harness itself.
type textLogger struct {
	mu     *sync.Mutex
	w      io.Writer
	runID  string
	node   NodeID
	prefix []Field
}

// NewLogger writes human-greppable structured records to w. Every record
// includes the run identity and (optionally) the local node id.
func NewLogger(w io.Writer, runID string, node NodeID) Logger {
	if w == nil {
		w = io.Discard
	}
	return &textLogger{mu: &sync.Mutex{}, w: w, runID: runID, node: node}
}

func (l *textLogger) With(fields ...Field) Logger {
	cp := make([]Field, 0, len(l.prefix)+len(fields))
	cp = append(cp, l.prefix...)
	cp = append(cp, fields...)
	return &textLogger{mu: l.mu, w: l.w, runID: l.runID, node: l.node, prefix: cp}
}

func (l *textLogger) emit(level, msg string, fields []Field) {
	l.mu.Lock()
	defer l.mu.Unlock()
	fmt.Fprintf(l.w, "%s level=%s run_id=%q node=%q version=%s msg=%q",
		time.Now().UTC().Format(time.RFC3339Nano), level, l.runID, string(l.node), Version, msg)
	for _, f := range l.prefix {
		writeField(l.w, f)
	}
	for _, f := range fields {
		writeField(l.w, f)
	}
	l.w.Write([]byte{'\n'})
}

func writeField(w io.Writer, f Field) {
	switch v := f.V.(type) {
	case VC:
		fmt.Fprintf(w, " %s=%s", f.K, v.String())
	default:
		fmt.Fprintf(w, " %s=%v", f.K, v)
	}
}

func (l *textLogger) Info(msg string, f ...Field)  { l.emit("info", msg, f) }
func (l *textLogger) Warn(msg string, f ...Field)  { l.emit("warn", msg, f) }
func (l *textLogger) Error(msg string, f ...Field) { l.emit("error", msg, f) }

// StdLogger returns a logger writing to stderr for the standalone node binary.
func StdLogger(runID string, node NodeID) Logger {
	return NewLogger(os.Stderr, runID, node)
}
