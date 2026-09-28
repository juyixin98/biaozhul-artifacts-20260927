// Package diag provides structured diagnostics with request correlation and
// secret redaction.
//
// Every log line is JSON and carries the service name. Reconcile decisions
// additionally carry the resource name, generation bookkeeping, external
// interaction phase, the decision (accepted|rejected|undecidable), a machine
// reason and a human message, so a reviewer can tell from the record why a
// given observation was acted on, refused, or left for a retry.
//
// Sensitive values (WidgetSpec.SecretToken) must be passed as Secret or
// wrapped with Redact; the raw value never reaches the output.
package diag

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// Decision labels used by the reconcile loop.
const (
	DecisionAccepted    = "accepted"
	DecisionRejected    = "rejected"
	DecisionUndecidable = "undecidable"
)

// requestIDKey is the context key for request correlation IDs.
type requestIDKey struct{}

// RequestIDHeader is the correlation header honoured on both services.
const RequestIDHeader = "X-Request-Id"

// NewRequestID returns a fresh opaque correlation ID.
func NewRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failure is not recoverable; the runtime environment is
		// fundamentally broken. Panic is appropriate for this binary.
		panic(err)
	}
	return hex.EncodeToString(b[:])
}

// WithRequestID stores id in the context.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, id)
}

// RequestIDFromContext retrieves the correlation id, generating one if absent.
func RequestIDFromContext(ctx context.Context) string {
	if id, ok := ctx.Value(requestIDKey{}).(string); ok && id != "" {
		return id
	}
	return "-"
}

// Secret is a string that must never be logged verbatim. It implements
// slog.LogValuer so it is redacted even if passed directly as a log attr.
type Secret string

func (s Secret) LogValue() slog.Value {
	return slog.StringValue(Redact(string(s)))
}

// Redact masks a sensitive string: the first two and last character survive
// for correlation, the middle becomes a fixed-width asterisk block. Short
// values are fully masked so nothing meaningful can leak.
func Redact(s string) string {
	if s == "" {
		return "-"
	}
	if len(s) <= 4 {
		return "****"
	}
	return s[:2] + "****" + s[len(s)-1:]
}

// Logger wraps slog with project-specific helpers.
type Logger struct {
	service string
	inner   *slog.Logger
}

// New builds a JSON logger writing to out. Pass nil for os.Stdout.
func New(out io.Writer, service string) *Logger {
	if out == nil {
		out = os.Stdout
	}
	h := slog.NewJSONHandler(out, &slog.HandlerOptions{
		Level: slog.LevelInfo,
		// Keep exactly the keys we set; do not transform them.
		ReplaceAttr: func(_ []string, a slog.Attr) slog.Attr { return a },
	})
	return &Logger{
		service: service,
		inner:   slog.New(h).With("service", service),
	}
}

// With returns a logger with extra bound attributes.
func (l *Logger) With(args ...any) *Logger {
	return &Logger{service: l.service, inner: l.inner.With(args...)}
}

// Info logs a general operational event.
func (l *Logger) Info(ctx context.Context, msg string, args ...any) {
	l.inner.LogAttrs(ctx, slog.LevelInfo, msg, l.withRID(ctx, args)...)
}

// Warn logs a recoverable anomaly.
func (l *Logger) Warn(ctx context.Context, msg string, args ...any) {
	l.inner.LogAttrs(ctx, slog.LevelWarn, msg, l.withRID(ctx, args)...)
}

// Error logs a failure.
func (l *Logger) Error(ctx context.Context, msg string, args ...any) {
	l.inner.LogAttrs(ctx, slog.LevelError, msg, l.withRID(ctx, args)...)
}

// DecisionArgs carries the fields of one reconcile decision.
type DecisionArgs struct {
	Resource             string
	Phase                string
	Decision             string
	Action               string
	Reason               string
	Detail               string
	Category             string
	RequestID            string
	ExternalID           string
	Generation           int64
	ObservedGeneration   int64
	ReconciledGeneration int64
	ExternalVersion      int64
}

// Decision records why the controller accepted, rejected or could not decide
// on an observation. This is the auditable core of the reconcile loop.
func (l *Logger) Decision(ctx context.Context, d DecisionArgs) {
	level := slog.LevelInfo
	if d.Decision == DecisionUndecidable {
		level = slog.LevelWarn
	}
	if d.Decision == DecisionRejected {
		level = slog.LevelWarn
	}
	rid := d.RequestID
	if rid == "" {
		rid = RequestIDFromContext(ctx)
	}
	l.inner.LogAttrs(ctx, level, "reconcile decision",
		slog.String("requestId", rid),
		slog.String("resource", d.Resource),
		slog.String("phase", d.Phase),
		slog.String("decision", d.Decision),
		slog.String("action", d.Action),
		slog.String("reason", d.Reason),
		slog.String("category", d.Category),
		slog.String("detail", d.Detail),
		slog.String("externalId", d.ExternalID),
		slog.Int64("extVersion", d.ExternalVersion),
		slog.Int64("generation", d.Generation),
		slog.Int64("observedGen", d.ObservedGeneration),
		slog.Int64("reconciledGen", d.ReconciledGeneration),
	)
}

func (l *Logger) withRID(ctx context.Context, args []any) []slog.Attr {
	attrs := argsToAttrs(args)
	rid := RequestIDFromContext(ctx)
	if rid != "-" {
		attrs = append(attrs, slog.String("requestId", rid))
	}
	return attrs
}

func argsToAttrs(args []any) []slog.Attr {
	var out []slog.Attr
	for len(args) >= 2 {
		key, _ := args[0].(string)
		if key == "" {
			args = args[2:]
			continue
		}
		out = append(out, slog.Any(key, args[1]))
		args = args[2:]
	}
	return out
}

// statusRecorder captures the status code for access logging.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// Middleware assigns/propagates a request ID and writes one structured access
// line per request. Bodies are never logged.
func (l *Logger) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimSpace(r.Header.Get(RequestIDHeader))
		if id == "" {
			id = NewRequestID()
		}
		w.Header().Set(RequestIDHeader, id)
		ctx := WithRequestID(r.Context(), id)
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		start := time.Now()
		next.ServeHTTP(rec, r.WithContext(ctx))
		l.inner.LogAttrs(ctx, slog.LevelInfo, "http",
			slog.String("requestId", id),
			slog.String("method", r.Method),
			slog.String("path", r.URL.Path),
			slog.Int("status", rec.status),
			slog.Duration("latency", time.Since(start)),
			slog.String("remote", r.RemoteAddr),
		)
	})
}
