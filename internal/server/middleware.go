package server

import (
	"context"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"
)

type runIDKey struct{}

func runIDFromCtx(ctx context.Context) string {
	if v, ok := ctx.Value(runIDKey{}).(string); ok {
		return v
	}
	return ""
}

var seqCounter uint64

// runSeq returns a process-unique, sortable id without importing a UUID dep.
func runSeq() string {
	n := atomic.AddUint64(&seqCounter, 1)
	return fmt.Sprintf("run-%d-%06d", time.Now().UTC().UnixNano(), n)
}

// withRunID honors a client-supplied X-Run-Id (so clients can correlate a
// retry chain) or generates one. It also logs method/path/status and mirrors
// the id into the response header.
func (s *Server) withRunID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Run-Id")
		if id == "" {
			id = s.idCounter()
		}
		ctx := context.WithValue(r.Context(), runIDKey{}, id)
		// Mirror the run id on EVERY response (success or failure), even if a
		// handler writes the header itself later — Header.Set is idempotent.
		w.Header().Set("X-Run-Id", id)
		sw := &statusWriter{ResponseWriter: w, status: 200}
		start := time.Now()
		next.ServeHTTP(sw, r.WithContext(ctx))
		s.log.Info("http_request", map[string]any{
			"method": r.Method, "path": r.URL.Path,
			"status": sw.status, "dur_ms": time.Since(start).Milliseconds(),
		})
	})
}

// recoverPanic turns an unexpected panic into a typed internal error rather
// than dropping the connection.
func (s *Server) recoverPanic(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				s.log.Error("handler_panic", map[string]any{
					"method": r.Method, "path": r.URL.Path, "panic": fmt.Sprint(rec),
				})
				writeJSONWith(w, http.StatusInternalServerError, map[string]any{
					"error": map[string]any{
						"category": "internal",
						"code":     "panic",
						"message":  fmt.Sprintf("internal error: %v", rec),
					},
				}, runIDFromCtx(r.Context()))
			}
		}()
		next.ServeHTTP(w, r)
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}
