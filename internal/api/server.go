// Package api exposes the controller over HTTP using only the standard
// library. Every mutating response carries the request id (X-Request-Id),
// which also tags release rows and their event stream so a failed operation
// can be correlated end to end.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"rollctl/internal/adapter"
	"rollctl/internal/controller"
)

// Server wires the controller and simulator into HTTP routes.
type Server struct {
	ctl *controller.Controller
	sim *adapter.Simulator
	log *slog.Logger
	mux *http.ServeMux
}

// NewServer builds the API handler. sim may be nil if fault fixtures are not
// exposed (the core controller still works with any ProcessManager).
func NewServer(ctl *controller.Controller, sim *adapter.Simulator, log *slog.Logger) *Server {
	s := &Server{ctl: ctl, sim: sim, log: log, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler returns the root HTTP handler (with request-id + logging middleware).
func (s *Server) Handler() http.Handler {
	return s.withRequestID(s.recoverer(s.mux))
}

// ---------------------------------------------------------------- middleware

type ctxKey string

const reqIDKey ctxKey = "request_id"

func (s *Server) withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimSpace(r.Header.Get("X-Request-Id"))
		if id == "" {
			id = newReqID()
		}
		w.Header().Set("X-Request-Id", id)
		ctx := context.WithValue(r.Context(), reqIDKey, id)
		start := time.Now()
		ww := &statusWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(ww, r.WithContext(ctx))
		s.log.Info("http",
			"request_id", id, "method", r.Method, "path", r.URL.Path,
			"status", ww.status, "duration_ms", time.Since(start).Milliseconds())
	})
}

func (s *Server) recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				s.log.Error("panic", "request_id", reqIDFromCtx(r.Context()), "err", rec, "path", r.URL.Path)
				writeError(w, r, http.StatusInternalServerError, "internal_error", "internal server error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (sw *statusWriter) WriteHeader(code int) { sw.status = code; sw.ResponseWriter.WriteHeader(code) }

func reqIDFromCtx(ctx context.Context) string {
	if v, ok := ctx.Value(reqIDKey).(string); ok {
		return v
	}
	return ""
}

// ---------------------------------------------------------------- responses

type errBody struct {
	Error     string `json:"error"`
	Category  string `json:"category,omitempty"`
	RequestID string `json:"requestId"`
	Message   string `json:"message"`
}

func writeError(w http.ResponseWriter, r *http.Request, code int, category, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(errBody{Error: category, Category: category, RequestID: reqIDFromCtx(r.Context()), Message: msg})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		writeError(w, r, http.StatusBadRequest, "bad_request", "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

// mapControllerError translates controller sentinel errors into HTTP codes.
func mapControllerError(w http.ResponseWriter, r *http.Request, err error) {
	switch {
	case errors.Is(err, controller.ErrBadRequest):
		writeError(w, r, http.StatusBadRequest, "bad_request", err.Error())
	case errors.Is(err, controller.ErrWorkloadNotFound), errors.Is(err, controller.ErrReleaseNotFound):
		writeError(w, r, http.StatusNotFound, "not_found", err.Error())
	case errors.Is(err, controller.ErrActiveRelease):
		writeError(w, r, http.StatusConflict, "active_release", err.Error())
	case errors.Is(err, controller.ErrNoRollbackTarget):
		writeError(w, r, http.StatusUnprocessableEntity, "no_rollback_target", err.Error())
	default:
		writeError(w, r, http.StatusInternalServerError, "internal_error", err.Error())
	}
}
