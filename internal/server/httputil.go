package server

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"runtime/debug"
	"strings"
	"time"

	"placer/internal/logx"
	"placer/internal/version"
)

const maxBodyBytes = 4 << 20 // 4 MiB

// errorBody is the stable error envelope. Unknown failures are always
// surfaced as such — handlers never return 2xx for an error path.
type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
	RunID   string `json:"run_id,omitempty"`
	Version string `json:"version"`
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = msg
	b.Version = version.Version
	writeJSON(w, status, b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, `{"error":{"code":"encode_failed","message":"response encoding failed"}}`)
		return
	}
	b = append(b, '\n')
	w.WriteHeader(status)
	_, _ = w.Write(b)
}

func decodeBody(r *http.Request, v any) error {
	defer r.Body.Close()
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil {
		return err
	}
	if len(body) > maxBodyBytes {
		return errors.New("request body too large (limit 4MiB)")
	}
	dec := json.NewDecoder(strings.NewReader(string(body)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	if dec.More() {
		return errors.New("unexpected trailing JSON content")
	}
	return nil
}

// splitSubroute parses /prefix/{id}[/{action}]. A trailing slash is not
// accepted (no ambiguous route match).
func splitSubroute(path, prefix string) (id, action string, ok bool) {
	rest := strings.TrimPrefix(path, prefix)
	if rest == "" || strings.HasSuffix(rest, "/") {
		return "", "", false
	}
	parts := strings.Split(rest, "/")
	switch len(parts) {
	case 1:
		return parts[0], "", true
	case 2:
		return parts[0], parts[1], true
	default:
		return "", "", false
	}
}

func writeStoreError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, sql.ErrNoRows):
		writeError(w, http.StatusNotFound, "not_found", "resource does not exist")
	default:
		writeError(w, http.StatusBadRequest, "store_error", err.Error())
	}
}

// runLogger derives a run id (honouring an X-Run-Id request header so test
// runs can force correlation) and a component logger.
func (s *Server) runLogger(r *http.Request, component, kind string) (*logx.Logger, string) {
	runID := r.Header.Get("X-Run-Id")
	if runID == "" {
		runID = logx.NewRunID()
	}
	_ = kind
	return s.log.With(component, runID), runID
}

type statusRecorder struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	n, err := r.ResponseWriter.Write(b)
	r.bytes += n
	return n, err
}

// requestLogger emits one structured line per request with run correlation,
// version, path, status and latency.
func (s *Server) requestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		runID := r.Header.Get("X-Run-Id")
		if runID == "" {
			runID = responseRunID(r)
		}
		rec := &statusRecorder{ResponseWriter: w}
		next.ServeHTTP(rec, r)
		s.log.With("http", runID).Info("http_request", map[string]any{
			"method": r.Method, "path": r.URL.Path, "status": rec.status,
			"bytes": rec.bytes, "latency_ms": time.Since(start).Milliseconds(),
		})
	})
}

// responseRunID is a fallback correlator created per inbound request.
func responseRunID(r *http.Request) string {
	if v := r.Context().Value(runIDKey{}); v != nil {
		return v.(string)
	}
	id := logx.NewRunID()
	*r = *r.WithContext(context.WithValue(r.Context(), runIDKey{}, id))
	return id
}

type runIDKey struct{}

// recoverer converts panics into 500 error bodies and logs the stack; a
// crash must never be reported as success.
func (s *Server) recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				s.log.Error("panic", errors.New("panic recovered"), map[string]any{
					"panic": rec, "stack": string(debug.Stack()),
					"path": r.URL.Path,
				})
				writeError(w, http.StatusInternalServerError, "internal_error", "unexpected server error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}
