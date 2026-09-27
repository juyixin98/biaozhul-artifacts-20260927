// Package api exposes the replay engine over HTTP on localhost. The
// accepted surface is intentionally small:
//
//	POST /v1/replays            run an inline config (JSON body)
//	POST /v1/replays/fixtures   run a named fixture from a local directory
//	GET  /v1/runs               list archived runs
//	GET  /v1/runs/{id}          fetch one run's report
//	GET  /v1/runs/{id}/trace    fetch the decision trace
//	GET  /healthz               liveness
//
// No wire-BGP is accepted: only the synthetic event schema of the config
// package can create state.
package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"pathvector/internal/config"
	"pathvector/internal/ierr"
	"pathvector/internal/replay"
	"pathvector/internal/store"
)

// Server bundles the dependencies of the HTTP surface.
type Server struct {
	Runner      *replay.Runner
	Store       *store.Store
	FixtureDir  string
	MaxBodySize int64
}

// NewRouter wires the routes.
func (s *Server) NewRouter() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/replays", s.handleReplay)
	mux.HandleFunc("POST /v1/replays/fixtures", s.handleFixture)
	mux.HandleFunc("GET /v1/runs", s.handleList)
	mux.HandleFunc("GET /v1/runs/{id}", s.handleGet)
	mux.HandleFunc("GET /v1/runs/{id}/trace", s.handleTrace)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return logBodyLimit(mux, s.MaxBodySize)
}

func (s *Server) handleReplay(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			writeErr(w, ierr.Wrap(ierr.KindResourceExhausted, "api.readBody",
				"request body exceeds limit", err), "")
		} else {
			writeErr(w, ierr.Wrap(ierr.KindInvalidInput, "api.readBody", "cannot read request body", err), "")
		}
		return
	}
	res, err := s.Runner.Execute(r.Context(), body)
	if err != nil {
		if res != nil {
			// Hard engine failure: persist done; surface 4xx/5xx but keep
			// the run id so the caller can fetch the partial trace.
			writeJSONWithRun(w, statusForKind(ierr.Of(err)), res, err)
			return
		}
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleFixture(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name string `json:"name"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, r, ierr.Wrap(ierr.KindInvalidInput, "api.fixture", "invalid JSON request", err))
		return
	}
	if req.Name == "" || strings.ContainsAny(req.Name, `/\`) {
		writeError(w, r, ierr.New(ierr.KindInvalidInput, "api.fixture",
			"name must be a non-empty bare fixture id"))
		return
	}
	cfg, err := config.LoadFixture(s.FixtureDir, req.Name)
	if err != nil {
		writeError(w, r, err)
		return
	}
	res, err := s.Runner.ExecuteConfig(r.Context(), cfg, nil)
	if err != nil {
		if res != nil {
			writeJSONWithRun(w, statusForKind(ierr.Of(err)), res, err)
			return
		}
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeJSON(w, http.StatusOK, map[string]any{"runs": []any{}})
		return
	}
	rows, err := s.Store.ListRuns(r.Context(), 100)
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": rows})
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeError(w, r, ierr.New(ierr.KindNotFound, "api.getRun", "persistence disabled"))
		return
	}
	rec, err := s.Store.GetRun(r.Context(), r.PathValue("id"))
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"summary": rec.Summary,
		"error":   rec.ErrorDetail,
		"report":  json.RawMessage(rec.ReportJSON),
		"config":  json.RawMessage(rec.ConfigJSON),
	})
}

func (s *Server) handleTrace(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeError(w, r, ierr.New(ierr.KindNotFound, "api.getTrace", "persistence disabled"))
		return
	}
	trace, err := s.Store.GetTrace(r.Context(), r.PathValue("id"))
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": r.PathValue("id"), "trace": trace})
}

// statusForKind maps the stable error kinds onto HTTP status codes.
func statusForKind(k ierr.Kind) int {
	switch k {
	case ierr.KindInvalidInput:
		return http.StatusBadRequest
	case ierr.KindStateConflict:
		return http.StatusConflict
	case ierr.KindResourceExhausted:
		return http.StatusTooManyRequests
	case ierr.KindNotFound:
		return http.StatusNotFound
	default:
		return http.StatusInternalServerError
	}
}

type errBody struct {
	Error  string `json:"error"`
	Kind   string `json:"kind"`
	Detail string `json:"detail"`
	RunID  string `json:"run_id,omitempty"`
}

func writeError(w http.ResponseWriter, _ *http.Request, err error) {
	writeErr(w, err, "")
}

func writeJSONWithRun(w http.ResponseWriter, code int, res *replay.Result, err error) {
	writeErr(w, err, res.RunID)
}

func writeErr(w http.ResponseWriter, err error, runID string) {
	kind := ierr.Of(err)
	body := errBody{
		Error:  string(kind),
		Kind:   string(kind),
		Detail: err.Error(),
		RunID:  runID,
	}
	writeJSON(w, statusForKind(kind), body)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// logBodyLimit caps request bodies and gives an oversized upload a
// resource_exhausted classification instead of a generic failure.
func logBodyLimit(h http.Handler, max int64) http.Handler {
	if max <= 0 {
		max = 1 << 20
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, max)
		h.ServeHTTP(w, r)
	})
}
