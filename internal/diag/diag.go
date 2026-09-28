// Package diag carries request-scoped diagnostic state: the id attached to
// every log record and API error, the accepted/rejected/undecided outcome
// taxonomy, and payload redaction so logs never contain message bodies.
package diag

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"os"
	"time"
)

// Outcome is why the service did what it did with a request.
type Outcome string

const (
	// Accepted: the request was valid and was applied/routed.
	Accepted Outcome = "accepted"
	// Rejected: the request was well-formed but violated a fixed rule
	// (wildcard placement, optimistic concurrency, not found, ...).
	Rejected Outcome = "rejected"
	// Undecided: the service could not determine the result (storage failure,
	// unavailable dependency). Nothing was applied; the client may retry.
	Undecided Outcome = "undecided"
)

type ctxKey int

const requestIDKey ctxKey = 1

// NewRequestID returns a 16-byte hex id.
func NewRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is an environment failure; fall back to a
		// time-based id so requests stay distinguishable, mark it clearly.
		return "t-" + time.Now().Format("20060102T150405.000000000")
	}
	return hex.EncodeToString(b[:])
}

// WithRequestID stores the id on the context.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey, id)
}

// RequestID returns the id on the context, or "".
func RequestID(ctx context.Context) string {
	if v, ok := ctx.Value(requestIDKey).(string); ok {
		return v
	}
	return ""
}

// Fingerprint returns the hex SHA-256 of a payload, for correlation without
// retaining content.
func Fingerprint(payload []byte) string {
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

// MaxRedactedPayloadLen caps how many masked bytes the preview shows; it hints
// at size while never revealing content.
const MaxRedactedPayloadLen = 8

// Redact returns a safe description of a payload: byte length, hash and a
// fixed-width mask. It is the only thing allowed into logs about bodies.
func Redact(payload []byte) map[string]any {
	maskLen := len(payload)
	if maskLen > MaxRedactedPayloadLen {
		maskLen = MaxRedactedPayloadLen
	}
	return map[string]any{
		"bytes":       len(payload),
		"sha256":      Fingerprint(payload),
		"preview":     string(repeat('*', maskLen)),
		"truncated":   len(payload) > MaxRedactedPayloadLen,
	}
}

func repeat(b byte, n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = b
	}
	return out
}

// Event is one structured diagnostic record. It is logged as JSON and is the
// shape tests assert on.
type Event struct {
	Time      time.Time      `json:"time"`
	Level     string         `json:"level"`
	RequestID string         `json:"request_id,omitempty"`
	Component string         `json:"component"`
	Outcome   Outcome        `json:"outcome,omitempty"`
	Category  string         `json:"category,omitempty"` // fixed failure class
	Action    string         `json:"action"`
	KeyState  map[string]any `json:"key_state,omitempty"`
	Reason    string         `json:"reason"`
	Version   int64          `json:"version,omitempty"`
	MessageID string         `json:"message_id,omitempty"`
}

// MarshalLogObject makes Event usable with slog.Any.
func (e Event) LogValue() slog.Value {
	return slog.GroupValue(
		slog.String("request_id", e.RequestID),
		slog.String("component", e.Component),
		slog.String("action", e.Action),
		slog.String("reason", e.Reason),
	)
}

// Logger is a small wrapper ensuring every line carries component/request id.
type Logger struct {
	l *slog.Logger
}

// NewLogger builds a JSON structured logger at the given level ("debug",
// "info", "warn", "error").
func NewLogger(level string) *Logger {
	var lv slog.Level
	switch level {
	case "debug":
		lv = slog.LevelDebug
	case "warn":
		lv = slog.LevelWarn
	case "error":
		lv = slog.LevelError
	default:
		lv = slog.LevelInfo
	}
	h := slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: lv})
	return &Logger{l: slog.New(h)}
}

// Record logs one event at the level implied by its outcome.
func (lg *Logger) Record(ctx context.Context, ev Event) {
	if ev.Time.IsZero() {
		ev.Time = time.Now().UTC()
	}
	if ev.RequestID == "" {
		ev.RequestID = RequestID(ctx)
	}
	level := slog.LevelInfo
	if ev.Outcome == Rejected {
		level = slog.LevelWarn
	}
	if ev.Outcome == Undecided {
		level = slog.LevelError
	}
	args := []any{
		"component", ev.Component,
		"action", ev.Action,
		"reason", ev.Reason,
	}
	if ev.RequestID != "" {
		args = append(args, "request_id", ev.RequestID)
	}
	if ev.Outcome != "" {
		args = append(args, "outcome", string(ev.Outcome))
	}
	if ev.Category != "" {
		args = append(args, "category", ev.Category)
	}
	if ev.Version != 0 {
		args = append(args, "version", ev.Version)
	}
	if ev.MessageID != "" {
		args = append(args, "message_id", ev.MessageID)
	}
	if len(ev.KeyState) > 0 {
		// KeyState values are constructed by callers and must themselves be
		// redacted; Redact() is the contract for any payload-shaped value.
		buf, err := json.Marshal(ev.KeyState)
		if err == nil {
			args = append(args, "key_state", json.RawMessage(buf))
		}
	}
	lg.l.Log(ctx, level, ev.Reason, args...)
}
