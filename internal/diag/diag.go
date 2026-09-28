// Package diag provides request-scoped diagnostics: identifiers, structured
// records and sensitive-value redaction.
package diag

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"strings"
	"sync/atomic"
	"time"
)

// Redacted is substituted for any value whose key looks sensitive.
const Redacted = "[REDACTED]"

// sensitiveKeyFragments mark operator metadata that must never be printed.
var sensitiveKeyFragments = []string{
	"password", "passwd", "secret", "token", "api-key", "apikey",
	"community", "private-key", "privatekey",
}

// IsSensitiveKey reports whether a metadata key requires redaction.
func IsSensitiveKey(key string) bool {
	k := strings.ToLower(key)
	for _, f := range sensitiveKeyFragments {
		if strings.Contains(k, f) {
			return true
		}
	}
	return false
}

// RedactMeta returns a copy of meta with sensitive values masked.
func RedactMeta(meta map[string]string) map[string]string {
	if len(meta) == 0 {
		return nil
	}
	out := make(map[string]string, len(meta))
	for k, v := range meta {
		if IsSensitiveKey(k) {
			out[k] = Redacted
		} else {
			out[k] = v
		}
	}
	return out
}

// RequestID generates a request identifier: a short timestamp prefix plus
// eight random bytes, e.g. "req-20260928T101543Z-a1b2c3d4e5f60718".
func NewRequestID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		// rand.Read failing is not recoverable in practice; degrade to zeros
		// rather than panic inside a request path.
		return "req-" + time.Now().UTC().Format("20060102T150405Z") + "-0000000000000000"
	}
	return "req-" + time.Now().UTC().Format("20060102T150405Z") + "-" + hex.EncodeToString(b[:])
}

// Record is one structured diagnostic line emitted as JSON.
type Record struct {
	Time      string         `json:"time"`
	RequestID string         `json:"request_id,omitempty"`
	Event     string         `json:"event"`
	Verdict   string         `json:"verdict,omitempty"`
	TableVer  uint64         `json:"table_version,omitempty"`
	Target    string         `json:"target,omitempty"`
	Key       string         `json:"key,omitempty"`
	Detail    string         `json:"detail,omitempty"`
	State     map[string]any `json:"state,omitempty"`
}

// Logger emits JSON records, one per line, and assigns local sequence numbers
// so concurrent requests can be correlated with counter values.
type Logger struct {
	w    io.Writer
	ch   chan Record
	done chan struct{}
	n    atomic.Uint64
}

// NewLogger starts an asynchronous JSON logger writing to w (os.Stderr by
// default). Close drains it and blocks until queued records are flushed.
func NewLogger(w io.Writer) *Logger {
	if w == nil {
		w = os.Stderr
	}
	l := &Logger{w: w, ch: make(chan Record, 256), done: make(chan struct{})}
	go l.run()
	return l
}

func (l *Logger) run() {
	defer close(l.done)
	enc := json.NewEncoder(l.w)
	for r := range l.ch {
		_ = enc.Encode(r)
	}
}

// Log enqueues a record. Safe for concurrent use; never blocks callers when
// the buffer is full (the record is dropped, counted via event sequence).
func (l *Logger) Log(r Record) {
	if r.Time == "" {
		r.Time = time.Now().UTC().Format(time.RFC3339Nano)
	}
	seq := l.n.Add(1)
	if r.State == nil {
		r.State = map[string]any{}
	}
	r.State["seq"] = seq
	select {
	case l.ch <- r:
	default:
	}
}

// Close flushes queued records and stops the logger.
func (l *Logger) Close() { close(l.ch); <-l.done }
