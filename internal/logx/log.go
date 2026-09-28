// Package logx provides small structured logging helpers. Every diagnostic
// emitted here must be safe to print: values stored under sensitive keys
// (secret, token, password, credential, ...) are replaced with a redaction
// marker before serialization, recursively.
package logx

import (
	"encoding/json"
	"io"
	"os"
	"sync"
	"time"
)

// SensitiveKeys lists lower-cased key name fragments that mark a value as
// sensitive. Matching is substring-based on the lower-cased key so that
// e.g. "dbPassword", "client_secret" and "adminToken" are all masked.
var SensitiveKeys = []string{
	"secret", "token", "password", "credential", "apikey", "api_key",
}

const redacted = "***REDACTED***"

// Logger emits one JSON object per line. It is safe for concurrent use.
type Logger struct {
	mu  sync.Mutex
	w   io.Writer
	now func() time.Time
}

// New returns a logger writing to stderr by default.
func New(w io.Writer) *Logger {
	if w == nil {
		w = os.Stderr
	}
	return &Logger{w: w, now: time.Now}
}

// Entry is a structured diagnostic record.
type Entry struct {
	Time      string         `json:"ts"`
	Level     string         `json:"level"`
	Component string         `json:"component"`
	Msg       string         `json:"msg"`
	Fields    map[string]any `json:"fields,omitempty"`
}

func (l *Logger) log(level, component, msg string, fields map[string]any) {
	e := Entry{
		Time:      l.now().UTC().Format(time.RFC3339Nano),
		Level:     level,
		Component: component,
		Msg:       msg,
		Fields:    Redact(fields).(map[string]any),
	}
	b, err := json.Marshal(e)
	if err != nil {
		b = []byte(`{"level":"error","msg":"log serialization failed"}`)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = l.w.Write(append(b, '\n'))
}

// Info logs at info level.
func (l *Logger) Info(component, msg string, fields map[string]any) {
	l.log("info", component, msg, fields)
}

// Warn logs at warn level.
func (l *Logger) Warn(component, msg string, fields map[string]any) {
	l.log("warn", component, msg, fields)
}

// Error logs at error level.
func (l *Logger) Error(component, msg string, fields map[string]any) {
	l.log("error", component, msg, fields)
}

// IsSensitive reports whether key denotes sensitive data.
func IsSensitive(key string) bool {
	low := toLower(key)
	for _, sk := range SensitiveKeys {
		if contains(low, sk) {
			return true
		}
	}
	return false
}

func toLower(s string) string {
	b := make([]byte, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		b[i] = c
	}
	return string(b)
}

func contains(s, sub string) bool {
	if len(sub) == 0 {
		return true
	}
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

// Redact returns a copy of v with every sensitive value replaced. It handles
// map[string]any, map[string]string and []any structures; unknown leaf types
// are returned as-is.
func Redact(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			if IsSensitive(k) {
				out[k] = redacted
			} else {
				out[k] = Redact(val)
			}
		}
		return out
	case map[string]string:
		out := make(map[string]string, len(t))
		for k, val := range t {
			if IsSensitive(k) {
				out[k] = redacted
			} else {
				out[k] = val
			}
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i := range t {
			out[i] = Redact(t[i])
		}
		return out
	default:
		return v
	}
}

// RedactSpec returns a JSON-friendly redacted copy of an object spec. It is
// used both for logs and for serving specs to callers that lack the
// controller credential.
func RedactSpec(spec map[string]any) map[string]any {
	r := Redact(spec)
	if r == nil {
		return map[string]any{}
	}
	m, ok := r.(map[string]any)
	if !ok {
		return map[string]any{}
	}
	return m
}
