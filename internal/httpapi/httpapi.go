// Package httpapi exposes the replay service over HTTP:
//
//	POST /v1/replay      run a scenario, persist it, return the result
//	GET  /v1/runs        list stored runs
//	GET  /v1/runs/{id}   fetch a stored result
//	GET  /healthz        liveness
//
// Every response carries an X-Request-Id header; log lines include the
// same id so failures can be correlated. Member addresses in log lines
// are redacted.
package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"time"

	"igmpq/internal/cats"
	"igmpq/internal/redact"
	"igmpq/internal/replay"
	"igmpq/internal/store"
)

type ctxKey int

const ctxReqID ctxKey = 0

// Server is the HTTP front end.
type Server struct {
	st     *store.Store
	logger *log.Logger
	mux    *http.ServeMux
}

// New builds the handler graph.
func New(st *store.Store, logger *log.Logger) *Server {
	s := &Server{st: st, logger: logger, mux: http.NewServeMux()}
	s.mux.HandleFunc("POST /v1/replay", s.handleReplay)
	s.mux.HandleFunc("GET /v1/runs", s.handleListRuns)
	s.mux.HandleFunc("GET /v1/runs/{id}", s.handleGetRun)
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	return s
}

// ServeHTTP wraps the mux with the request-id/access-log middleware.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	reqID := newRequestID()
	start := time.Now()
	rw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
	rw.Header().Set("X-Request-Id", reqID)
	s.mux.ServeHTTP(rw, r.WithContext(context.WithValue(r.Context(), ctxReqID, reqID)))
	s.logger.Printf("req=%s %s %s -> %d (%s)", reqID, r.Method, r.URL.Path, rw.status, time.Since(start).Round(time.Microsecond))
}

func reqID(r *http.Request) string {
	if id, ok := r.Context().Value(ctxReqID).(string); ok {
		return id
	}
	return "req-unknown"
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func newRequestID() string {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		return "req-unknown"
	}
	return "req-" + hex.EncodeToString(b)
}

func (s *Server) handleReplay(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		s.writeError(w, r, http.StatusBadRequest, cats.New(cats.BadRequest, "could not read request body"))
		return
	}
	sc, err := replay.LoadFile(body)
	if err != nil {
		s.writeError(w, r, http.StatusBadRequest, err)
		return
	}
	res, err := replay.Run(sc)
	if err != nil {
		s.writeError(w, r, http.StatusUnprocessableEntity, err)
		return
	}
	runID, err := s.st.SaveRun(r.Context(), sc, res)
	if err != nil {
		s.writeError(w, r, http.StatusInternalServerError, err)
		return
	}
	res.RunID = runID
	for _, rj := range res.Rejections {
		s.logger.Printf("req=%s run=%d rejection event=%d category=%s member=%s",
			reqID(r), runID, rj.EventIndex, rj.Category, redact.IP(rj.Member))
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleListRuns(w http.ResponseWriter, r *http.Request) {
	runs, err := s.st.ListRuns(r.Context())
	if err != nil {
		s.writeError(w, r, http.StatusInternalServerError, err)
		return
	}
	if runs == nil {
		runs = []store.RunMeta{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": runs})
}

func (s *Server) handleGetRun(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		s.writeError(w, r, http.StatusBadRequest, cats.New(cats.BadRequest, "run id must be an integer"))
		return
	}
	res, err := s.st.LoadResult(r.Context(), id)
	if err != nil {
		status := http.StatusInternalServerError
		if cats.CategoryOf(err) == cats.RunNotFound {
			status = http.StatusNotFound
		}
		s.writeError(w, r, status, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) writeError(w http.ResponseWriter, r *http.Request, status int, err error) {
	var ce *cats.Error
	if !errors.As(err, &ce) {
		ce = cats.New(cats.Internal, err.Error())
	}
	s.logger.Printf("req=%s %s %s -> %d category=%s msg=%q",
		reqID(r), r.Method, r.URL.Path, status, ce.Category, ce.Message)
	writeJSON(w, status, map[string]any{"error": ce})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}
