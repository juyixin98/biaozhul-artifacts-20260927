package diag

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
)

type ctxKey int

const requestIDKey ctxKey = iota

// RequestIDHeader is the inbound/outbound header carrying correlation IDs.
const RequestIDHeader = "X-Request-ID"

// NewRequestID returns a random opaque identifier.
func NewRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// rand.Read failing on linux is effectively unreachable; fall back
		// to a constant marker rather than panic in request handling.
		return "req-unknown"
	}
	return "req-" + hex.EncodeToString(b[:])
}

// WithRequestID stores id on the context.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey, id)
}

// RequestIDFromContext returns the stored request id, or "".
func RequestIDFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(requestIDKey).(string); ok {
		return v
	}
	return ""
}

// RequestIDMiddleware honors an inbound X-Request-ID or mints a new one,
// puts it on the context, and echoes it back on the response.
func RequestIDMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get(RequestIDHeader)
		if id == "" {
			id = NewRequestID()
		}
		w.Header().Set(RequestIDHeader, id)
		next.ServeHTTP(w, r.WithContext(WithRequestID(r.Context(), id)))
	})
}
