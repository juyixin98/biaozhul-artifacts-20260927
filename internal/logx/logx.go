// Package logx is the coordinator's tiny structured logger. Every log line is
// key=value quoted text carrying a request id (when the work was triggered by
// a request) and the minimum state needed to explain a decision. Values are
// passed through Redact when they come from label sets or request bodies, so
// secrets carried in labels (tokens, credentials) are never printed raw.
package logx

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// Logger serializes one-line structured records to one writer.
type Logger struct {
	mu sync.Mutex
	w  io.Writer
}

func New(w io.Writer) *Logger {
	if w == nil {
		w = os.Stderr
	}
	return &Logger{w: w}
}

// Field is one structured value. Sensitive marks a value that must be
// redacted before formatting.
type Field struct {
	Key       string
	Value     string
	Sensitive bool
}

func F(k, v string) Field       { return Field{Key: k, Value: v} }
func S(k, v string) Field       { return Field{Key: k, Value: v, Sensitive: true} }
func I(k string, v int) Field   { return Field{Key: k, Value: fmt.Sprintf("%d", v)}}
func I64(k string, v int64) Field { return Field{Key: k, Value: fmt.Sprintf("%d", v)}}

// Event writes one record. level is "info"|"warn"|"error". msg is the fixed
// machine-readable event name; requestID may be empty.
func (l *Logger) Event(level, msg, requestID string, fields ...Field) {
	var sb strings.Builder
	sb.WriteString(time.Now().UTC().Format(time.RFC3339Nano))
	sb.WriteString(" level=")
	sb.WriteString(level)
	sb.WriteString(" event=")
	sb.WriteString(quote(msg))
	if requestID != "" {
		sb.WriteString(" request_id=")
		sb.WriteString(quote(requestID))
	}
	sort.SliceStable(fields, func(i, j int) bool { return fields[i].Key < fields[j].Key })
	for _, f := range fields {
		sb.WriteByte(' ')
		sb.WriteString(f.Key)
		sb.WriteByte('=')
		if f.Sensitive {
			sb.WriteString(quote(Redact(f.Value)))
		} else {
			sb.WriteString(quote(f.Value))
		}
	}
	sb.WriteByte('\n')
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = io.WriteString(l.w, sb.String())
}

func quote(s string) string {
	if s == "" {
		return `""`
	}
	if strings.ContainsAny(s, " \t\"=\n") {
		return "\"" + strings.ReplaceAll(s, "\"", "\\\"") + "\""
	}
	return s
}

// sensitiveKeyParts marks label/parameter names whose VALUES must never be
// logged. The key itself is preserved (it is not secret), only the value is
// masked.
var sensitiveKeyParts = []string{"token", "secret", "password", "passwd", "credential", "apikey", "api-key", "private-key"}

// IsSensitiveKey reports whether a label/parameter name implies a secret value.
func IsSensitiveKey(k string) bool {
	lk := strings.ToLower(k)
	for _, p := range sensitiveKeyParts {
		if strings.Contains(lk, p) {
			return true
		}
	}
	return false
}

// Redact masks a potentially sensitive scalar value.
func Redact(v string) string {
	if v == "" {
		return ""
	}
	return "***REDACTED" + fmt.Sprintf("(len=%d)", len(v)) + "***"
}

// SafeLabels renders labels for diagnostics with sensitive values masked.
// Non-sensitive values are kept verbatim because they are needed to explain
// selector-mismatch decisions.
func SafeLabels(m map[string]string) string {
	if len(m) == 0 {
		return ""
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		if IsSensitiveKey(k) {
			parts = append(parts, k+"="+Redact(m[k]))
		} else {
			parts = append(parts, k+"="+m[k])
		}
	}
	return strings.Join(parts, ",")
}

// NewRequestID returns an unprivileged correlation id. It is NOT a secret;
// it exists so every accept/reject/undecidable line can be joined to the
// diagnostic row and the HTTP response.
func NewRequestID() string {
	var b [12]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "req-unknown"
	}
	return "req-" + hex.EncodeToString(b[:])
}
