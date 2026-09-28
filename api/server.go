// Package api exposes the replay backend over localhost HTTP.
//
// Endpoints (see README for examples):
//
//	POST /runs                 submit a scenario, run it, persist + return
//	GET  /runs                 list recent runs
//	GET  /runs/{id}            get run summary / full best-path result
//	GET  /runs/{id}/traces     ordered trace (loop suppression, policy, …)
//	GET  /runs/{id}/decisions  best-path decision history
//	GET  /runs/{id}/deliveries external events as delivered (replay order)
//	GET  /runs/{id}/scenario   exact submitted scenario bytes
//	POST /runs/{id}/replay     re-run the stored scenario as a new run
//	GET  /healthz              liveness
package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"pvsim/model"
	"pvsim/replay"
	"pvsim/store"
)

// Server wires HTTP routes to a replay.Service.
type Server struct {
	svc *replay.Service
	st  *store.Store
	mux *http.ServeMux
}

// NewServer builds the HTTP handler.
func NewServer(svc *replay.Service, st *store.Store) *Server {
	s := &Server{svc: svc, st: st, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	s.mux.HandleFunc("/runs", s.handleRuns)
	s.mux.HandleFunc("/runs/", s.handleRunByID)
}

func (s *Server) handleRuns(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	switch r.Method {
	case http.MethodPost:
		raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, replay.MaxPayloadBytes))
		if err != nil {
			var mbe *http.MaxBytesError
			if errors.As(err, &mbe) {
				writeModelError(w, model.NewError(model.KindResourceExhausted, "PAYLOAD_TOO_LARGE",
					"scenario payload exceeds %d byte limit", replay.MaxPayloadBytes))
				return
			}
			writeModelError(w, model.NewError(model.KindInput, "PAYLOAD_UNREADABLE",
				"read body: %v", err))
			return
		}
		sum, _, err := s.svc.Submit(ctx, raw, "")
		if err != nil {
			writeModelError(w, err)
			return
		}
		writeJSON(w, http.StatusCreated, sum)
	case http.MethodGet:
		runs, err := s.st.ListRuns(ctx, 100)
		if err != nil {
			writeModelError(w, model.NewError(model.KindComputeFailed, "PERSISTENCE", "%v", err))
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"runs": runs})
	default:
		w.Header().Set("Allow", "GET, POST")
		writeModelError(w, model.NewError(model.KindInput, "METHOD_NOT_ALLOWED",
			"method %s not allowed", r.Method))
	}
}

func (s *Server) handleRunByID(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	path := strings.TrimPrefix(r.URL.Path, "/runs/")
	parts := strings.Split(path, "/")
	if len(parts) == 0 || parts[0] == "" {
		http.Redirect(w, r, "/runs", http.StatusTemporaryRedirect)
		return
	}
	id := parts[0]
	sub := ""
	if len(parts) > 1 {
		sub = parts[1]
	}
	if len(parts) > 2 {
		writeModelError(w, model.NewError(model.KindNotFound, "NO_SUCH_ENDPOINT",
			"unknown path %q", r.URL.Path))
		return
	}

	switch {
	case sub == "" && r.Method == http.MethodGet:
		sum, res, err := s.svc.Get(ctx, id)
		if err != nil {
			writeModelError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"summary": sum, "result": res})
	case sub == "replay" && r.Method == http.MethodPost:
		sum, _, err := s.svc.Replay(ctx, id)
		if err != nil {
			writeModelError(w, err)
			return
		}
		writeJSON(w, http.StatusCreated, sum)
	case sub == "traces" && r.Method == http.MethodGet:
		rows, err := s.st.GetTraces(ctx, id)
		if err != nil {
			writeModelError(w, mapStoreErr(id, err))
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"run_id": id, "traces": rows})
	case sub == "decisions" && r.Method == http.MethodGet:
		rows, err := s.st.GetDecisions(ctx, id)
		if err != nil {
			writeModelError(w, mapStoreErr(id, err))
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"run_id": id, "decisions": rows})
	case sub == "deliveries" && r.Method == http.MethodGet:
		rows, err := s.st.GetDeliveries(ctx, id)
		if err != nil {
			writeModelError(w, mapStoreErr(id, err))
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"run_id": id, "deliveries": rows})
	case sub == "scenario" && r.Method == http.MethodGet:
		raw, err := s.st.RunScenarioJSON(ctx, id)
		if err != nil {
			writeModelError(w, mapStoreErr(id, err))
			return
		}
		// Return the stored scenario verbatim so it can be replayed exactly.
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(raw)
	default:
		w.Header().Set("Allow", "GET, POST")
		writeModelError(w, model.NewError(model.KindInput, "METHOD_NOT_ALLOWED",
			"method %s not allowed on %q", r.Method, r.URL.Path))
	}
}

func mapStoreErr(id string, err error) error {
	if errors.Is(err, store.ErrNotFound) {
		return model.NewError(model.KindNotFound, "RUN_NOT_FOUND", "no run with id %q", id)
	}
	var me *model.Error
	if errors.As(err, &me) {
		return err
	}
	return model.NewError(model.KindComputeFailed, "PERSISTENCE", "%v", err)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeModelError(w http.ResponseWriter, err error) {
	var me *model.Error
	if !errors.As(err, &me) {
		me = model.NewError(model.KindComputeFailed, "INTERNAL", "%v", err)
	}
	status := httpStatusFor(me.Kind)
	writeJSON(w, status, map[string]any{
		"error": map[string]string{
			"kind":    string(me.Kind),
			"code":    me.Code,
			"message": me.Message,
		},
	})
}

func httpStatusFor(k model.Kind) int {
	switch k {
	case model.KindInput:
		return http.StatusBadRequest // 400
	case model.KindStateConflict:
		return http.StatusConflict // 409
	case model.KindResourceExhausted:
		return http.StatusUnprocessableEntity // 422 (budget exhaustion semantics)
	case model.KindNotFound:
		return http.StatusNotFound // 404
	default:
		return http.StatusInternalServerError // 500
	}
}
