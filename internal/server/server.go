// Package server is the stdlib HTTP adapter exposing the stateless
// scheduler endpoints and the cluster inventory, plus a reconcile trigger.
//
// Endpoints:
//
//	GET  /healthz
//	GET  /version
//	POST /v1/plans                 stateless batch placement
//	POST /v1/replacements          stateless rolling replacement plan
//	GET  /v1/nodes                 inventory
//	PUT  /v1/nodes/{id}            upsert node
//	POST /v1/nodes/{id}/status     ready|disabled|not_ready
//	DELETE /v1/nodes/{id}
//	GET  /v1/instances             inventory
//	PUT  /v1/instances/{id}        upsert instance
//	DELETE /v1/instances/{id}
//	GET  /v1/policy
//	PUT  /v1/policy
//	POST /v1/reconcile             run one pass against the store
//	GET  /v1/runs/{runID}          stored run + result
//	GET  /v1/runs/{runID}/events   durable event stream
//
// All plan/replace responses are deterministic and include a run id that
// correlates with structured log lines and persisted rows.
package server

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"placer/internal/logx"
	"placer/internal/model"
	"placer/internal/reconcile"
	"placer/internal/scheduler"
	"placer/internal/store"
	"placer/internal/version"
)

// Server wires store, reconcile loop and logger into HTTP handlers.
type Server struct {
	st   *store.Store
	loop *reconcile.Loop
	log  *logx.Logger
}

// New builds the server.
func New(st *store.Store, loop *reconcile.Loop, log *logx.Logger) *Server {
	return &Server{st: st, loop: loop, log: log}
}

// Routes returns the configured mux.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.healthz)
	mux.HandleFunc("/version", s.versionHandler)
	mux.HandleFunc("/v1/plans", s.plan)
	mux.HandleFunc("/v1/replacements", s.replacements)
	mux.HandleFunc("/v1/nodes", s.nodes)
	mux.HandleFunc("/v1/nodes/", s.nodeByID)
	mux.HandleFunc("/v1/instances", s.instances)
	mux.HandleFunc("/v1/instances/", s.instanceByID)
	mux.HandleFunc("/v1/policy", s.policy)
	mux.HandleFunc("/v1/reconcile", s.reconcileHandler)
	mux.HandleFunc("/v1/runs/", s.runs)
	return s.recoverer(s.requestLogger(mux))
}

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) versionHandler(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"version": version.Version, "commit": version.Commit, "build_time": version.BuildTime,
	})
}

func (s *Server) plan(w http.ResponseWriter, r *http.Request) {
	runLog, runID := s.runLogger(r, "http", "plan")
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	var req model.PlanRequest
	if err := decodeBody(r, &req); err != nil {
		runLog.Error("bad_request", err, nil)
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	req.RunID = runID
	res, err := scheduler.Plan(req)
	if err != nil {
		var exhausted *scheduler.SearchExhausted
		if errors.As(err, &exhausted) {
			runLog.Error("search_exhausted", err, nil)
			writeError(w, http.StatusUnprocessableEntity, "search_exhausted", err.Error())
			return
		}
		runLog.Error("plan_invalid", err, nil)
		writeError(w, http.StatusBadRequest, "invalid_plan", err.Error())
		return
	}
	status := http.StatusOK
	if !res.Feasible {
		status = http.StatusConflict
	}
	runLog.Info("plan_done", map[string]any{
		"feasible": res.Feasible, "solver": res.Solver,
		"conflicts": len(res.Conflicts), "decisions": len(res.Decisions),
	})
	writeJSON(w, status, res)
}

func (s *Server) replacements(w http.ResponseWriter, r *http.Request) {
	runLog, runID := s.runLogger(r, "http", "replace")
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	var req model.ReplaceRequest
	if err := decodeBody(r, &req); err != nil {
		runLog.Error("bad_request", err, nil)
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	req.RunID = runID
	res, err := scheduler.Replace(req)
	if err != nil {
		var exhausted *scheduler.SearchExhausted
		if errors.As(err, &exhausted) {
			runLog.Error("search_exhausted", err, nil)
			writeError(w, http.StatusUnprocessableEntity, "search_exhausted", err.Error())
			return
		}
		runLog.Error("replace_invalid", err, nil)
		writeError(w, http.StatusBadRequest, "invalid_replace", err.Error())
		return
	}
	status := http.StatusOK
	if !res.Feasible {
		status = http.StatusConflict
	}
	runLog.Info("replace_done", map[string]any{
		"feasible": res.Feasible, "conflicts": len(res.Conflicts), "ops": len(res.Ops),
	})
	writeJSON(w, status, res)
}

// ---- inventory handlers ----

func (s *Server) nodes(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	snap, err := s.st.LoadSnapshot(ctx)
	if err != nil {
		s.log.Error("snapshot_failed", err, nil)
		writeError(w, http.StatusInternalServerError, "store_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"nodes": snap.Nodes})
}

func (s *Server) nodeByID(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	id, action, ok := splitSubroute(r.URL.Path, "/v1/nodes/")
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", r.URL.Path)
		return
	}
	switch {
	case action == "" && r.Method == http.MethodPut:
		var n model.Node
		if err := decodeBody(r, &n); err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
		n.ID = id
		if err := s.st.UpsertNode(ctx, n); err != nil {
			writeError(w, http.StatusBadRequest, "invalid_node", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, n)
	case action == "" && r.Method == http.MethodDelete:
		if err := s.st.DeleteNode(ctx, id); err != nil {
			writeStoreError(w, err)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	case action == "status" && r.Method == http.MethodPost:
		var body struct {
			Status model.NodeStatus `json:"status"`
		}
		if err := decodeBody(r, &body); err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
		if err := s.st.SetNodeStatus(ctx, id, body.Status); err != nil {
			writeStoreError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"id": id, "status": string(body.Status)})
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "unsupported node route")
	}
}

func (s *Server) instances(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	snap, err := s.st.LoadSnapshot(ctx)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"instances": snap.Instances})
}

func (s *Server) instanceByID(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	id, action, ok := splitSubroute(r.URL.Path, "/v1/instances/")
	if !ok || action != "" {
		writeError(w, http.StatusNotFound, "not_found", r.URL.Path)
		return
	}
	switch r.Method {
	case http.MethodPut:
		var in model.Instance
		if err := decodeBody(r, &in); err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
		in.ID = id
		if err := s.st.UpsertInstance(ctx, in); err != nil {
			writeError(w, http.StatusBadRequest, "invalid_instance", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, in)
	case http.MethodDelete:
		if err := s.st.DeleteInstance(ctx, id); err != nil {
			writeStoreError(w, err)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use PUT or DELETE")
	}
}

func (s *Server) policy(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	switch r.Method {
	case http.MethodGet:
		snap, err := s.st.LoadSnapshot(ctx)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "store_error", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, snap.Policy)
	case http.MethodPut:
		var p model.Policy
		if err := decodeBody(r, &p); err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
		if err := s.st.SetPolicy(ctx, p); err != nil {
			writeError(w, http.StatusBadRequest, "invalid_policy", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, p)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET or PUT")
	}
}

func (s *Server) reconcileHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	runID := r.URL.Query().Get("run_id")
	if runID == "" {
		runID = logx.NewRunID()
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	sum, err := s.loop.RunOnce(ctx, runID)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{
			"run_id": runID, "status": "error", "err": err.Error(),
		})
		return
	}
	status := http.StatusOK
	if sum.Status == reconcile.StatusConflict {
		status = http.StatusConflict
	}
	writeJSON(w, status, map[string]any{
		"run_id":    sum.RunID,
		"status":    string(sum.Status),
		"nodes":     sum.Nodes,
		"pending":   sum.Pending,
		"decisions": sum.Decisions,
		"conflicts": sum.Conflicts,
		"objective": sum.Objective,
		"solver":    sum.Solver,
	})
}

func (s *Server) runs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	rest := strings.TrimPrefix(r.URL.Path, "/v1/runs/")
	parts := strings.Split(rest, "/")
	if len(parts) == 0 || parts[0] == "" {
		writeError(w, http.StatusNotFound, "not_found", "run id required")
		return
	}
	runID := parts[0]
	kind, status, err := s.st.RunStatus(ctx, runID)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	if len(parts) == 2 && parts[1] == "events" {
		evs, err := s.st.Events(ctx, runID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "store_error", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"run_id": runID, "events": evs})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"run_id": runID, "kind": kind, "status": status})
}
