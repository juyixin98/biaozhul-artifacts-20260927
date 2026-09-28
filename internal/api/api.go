// Package api is the net/http adapter exposing the placement engine over a
// small JSON API. It contains no scheduling logic — only request decoding,
// status mapping, correlation headers and error envelopes.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"opp284/placement/internal/config"
	"opp284/placement/internal/engine"
	"opp284/placement/internal/logging"
	"opp284/placement/internal/model"
	"opp284/placement/internal/reconcile"
	"opp284/placement/internal/scheduler"
	"opp284/placement/internal/store"
	"opp284/placement/internal/version"
)

// Server bundles dependencies for the HTTP routes.
type Server struct {
	eng       *engine.Engine
	st        *store.Store
	rec       *reconcile.Reconciler
	log       *logging.Logger
	clusterID string
}

// NewServer constructs the API server. rec may be nil (POST /reconcile then
// reports 503).
func NewServer(eng *engine.Engine, st *store.Store, rec *reconcile.Reconciler,
	log *logging.Logger, clusterID string) *Server {
	return &Server{eng: eng, st: st, rec: rec, log: log, clusterID: clusterID}
}

// Routes returns the wired mux.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /clusters/", s.handleGetCluster)
	mux.HandleFunc("POST /clusters/", s.handleUpsertCluster)
	mux.HandleFunc("POST /plans", s.handleCreatePlan)
	mux.HandleFunc("GET /plans", s.handleListPlans)
	mux.HandleFunc("GET /plans/", s.handleGetPlan)
	mux.HandleFunc("POST /reconcile", s.handleReconcile)
	return s.withMiddleware(mux)
}

// withMiddleware injects X-Request-Id (honoring a client-supplied id), logs
// every request, and never panics into a silent 200: panics become 500.
func (s *Server) withMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reqID := r.Header.Get("X-Request-Id")
		if reqID == "" {
			reqID = engine.NewRequestID()
		}
		w.Header().Set("X-Request-Id", reqID)
		w.Header().Set("X-Run-Id", s.log.RunID())
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: 200}
		defer func() {
			if rec := recover(); rec != nil {
				s.log.Fail("panic recovered", map[string]any{
					"request_id": reqID, "path": r.URL.Path, "panic": rec,
				})
				writeError(sw, http.StatusInternalServerError, model.ReasonInternal, "internal error", nil)
			}
			level := "ok"
			if sw.status >= 500 {
				level = "failed"
			} else if sw.status >= 400 {
				level = "degraded"
			}
			s.log.Info("http request", map[string]any{
				"request_id": reqID, "method": r.Method, "path": r.URL.Path,
				"status": sw.status, "dur_ms": time.Since(start).Milliseconds(), "level_status": level,
			})
		}()
		ctx := context.WithValue(r.Context(), reqIDKey{}, reqID)
		next.ServeHTTP(sw, r.WithContext(ctx))
	})
}

type reqIDKey struct{}

func reqID(r *http.Request) string {
	if v, ok := r.Context().Value(reqIDKey{}).(string); ok && v != "" {
		return v
	}
	return engine.NewRequestID()
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok", "service": "placement",
		"version": version.Version, "commit": version.Commit,
		"built_at": version.BuiltAt, "version_full": version.String(),
		"run_id":   s.log.RunID(),
	})
}

// --- Cluster DTOs -----------------------------------------------------------

type nodeDTO struct {
	ID       string            `json:"id"`
	Zone     string            `json:"zone"`
	Capacity model.Resources   `json:"capacity"`
	Used     model.Resources   `json:"used,omitempty"`
	Labels   model.Labels      `json:"labels,omitempty"`
	Eligible bool              `json:"eligible"`
}

type runningDTO struct {
	ID             string          `json:"id"`
	NodeID         string          `json:"node_id"`
	Request        model.Resources `json:"request"`
	AffinityGroups []string        `json:"affinity_groups,omitempty"`
}

type clusterDTO struct {
	ID            string             `json:"id"`
	DeclaredZones []string           `json:"declared_zones,omitempty"`
	Nodes         []nodeDTO          `json:"nodes"`
	Running       []runningDTO       `json:"running,omitempty"`
	Groups        []model.Group      `json:"groups,omitempty"`
}

func (s *Server) clusterIDFromPath(r *http.Request) string {
	// /clusters/{id}  (Go 1.22 method-pattern mux; parse manually for clarity)
	p := strings.TrimPrefix(r.URL.Path, "/clusters/")
	if i := strings.IndexByte(p, '/'); i >= 0 {
		p = p[:i]
	}
	return p
}

func (s *Server) handleUpsertCluster(w http.ResponseWriter, r *http.Request) {
	id := s.clusterIDFromPath(r)
	if id == "" {
		writeError(w, http.StatusBadRequest, model.ReasonInternal, "cluster id required", nil)
		return
	}
	var dto clusterDTO
	if err := decodeStrict(r, &dto); err != nil {
		writeError(w, http.StatusBadRequest, model.ReasonInternal, "invalid JSON: "+err.Error(), nil)
		return
	}
	nodes := make([]model.Node, 0, len(dto.Nodes))
	for _, n := range dto.Nodes {
		nodes = append(nodes, model.Node{
			ID: n.ID, Zone: n.Zone, Capacity: n.Capacity, Used: n.Used,
			Labels: n.Labels, Eligible: n.Eligible,
		})
	}
	running := make([]store.RunningRecord, 0, len(dto.Running))
	for _, rn := range dto.Running {
		running = append(running, store.RunningRecord{
			ID: rn.ID, NodeID: rn.NodeID, Request: rn.Request, Groups: rn.AffinityGroups,
		})
	}
	if err := s.st.UpsertCluster(r.Context(), id, dto.DeclaredZones, nodes, running, dto.Groups); err != nil {
		writeError(w, http.StatusBadRequest, model.ReasonInternal, err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "cluster_id": id, "nodes": len(nodes)})
}

func (s *Server) handleGetCluster(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, model.ReasonInternal, "method not allowed", nil)
		return
	}
	id := s.clusterIDFromPath(r)
	nodes, running, groups, zones, err := s.st.LoadCluster(r.Context(), id)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, model.ReasonInternal, "cluster not found", nil)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, model.ReasonInternal, err.Error(), nil)
		return
	}
	dto := clusterDTO{ID: id, DeclaredZones: zones, Groups: groups}
	for _, n := range nodes {
		dto.Nodes = append(dto.Nodes, nodeDTO{
			ID: n.ID, Zone: n.Zone, Capacity: n.Capacity, Used: n.Used,
			Labels: n.Labels, Eligible: n.Eligible,
		})
	}
	for _, rn := range running {
		dto.Running = append(dto.Running, runningDTO{
			ID: rn.ID, NodeID: rn.NodeID, Request: rn.Request, AffinityGroups: rn.Groups,
		})
	}
	writeJSON(w, http.StatusOK, dto)
}

// --- Plans ------------------------------------------------------------------

type planRequest struct {
	ClusterID     string                   `json:"cluster_id"`
	AllowRecreate bool                     `json:"allow_recreate"`
	Intents       []struct {
		ID             string            `json:"id"`
		Request        model.Resources   `json:"request"`
		RequiredZone   string            `json:"required_zone,omitempty"`
		NodeSelector   model.Selector    `json:"node_selector,omitempty"`
		AffinityGroups []string          `json:"affinity_groups,omitempty"`
	} `json:"intents"`
	Replacements map[string]struct {
		OldID string `json:"old_id"`
	} `json:"replacements,omitempty"`
}

func (s *Server) handleCreatePlan(w http.ResponseWriter, r *http.Request) {
	var pr planRequest
	if err := decodeStrict(r, &pr); err != nil {
		writeError(w, http.StatusBadRequest, model.ReasonInternal, "invalid JSON: "+err.Error(), nil)
		return
	}
	if pr.ClusterID == "" {
		pr.ClusterID = s.clusterID
	}
	in := engine.PlanInput{ClusterID: pr.ClusterID, AllowRecreate: pr.AllowRecreate}
	for _, it := range pr.Intents {
		in.Intents = append(in.Intents, model.Intent{
			ID: it.ID, Request: it.Request, RequiredZone: it.RequiredZone,
			NodeSelector: it.NodeSelector, AffinityGroups: it.AffinityGroups,
		})
	}
	if len(pr.Replacements) > 0 {
		in.Replacements = map[string]scheduler.Replacement{}
		for k, v := range pr.Replacements {
			in.Replacements[k] = scheduler.Replacement{OldID: v.OldID}
		}
	}
	out, err := s.eng.SolveAndSave(r.Context(), reqID(r), in)
	if err != nil {
		writeError(w, http.StatusInternalServerError, model.ReasonInternal, err.Error(), nil)
		return
	}
	if !out.Success {
		writeFailure(w, http.StatusConflict, out.PlanID, out.Failure, out.Steps)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"plan_id":     out.PlanID,
		"status":      "succeeded",
		"placements":  out.Decision.Placements,
		"score":       out.Decision.Score,
		"exhaustive":  out.Decision.Exhaustive,
		"leaf_visits": out.Decision.LeafVisits,
		"domains":     out.Decision.ParticipatingDomains,
		"steps":       out.Steps,
	})
}

func (s *Server) handleListPlans(w http.ResponseWriter, r *http.Request) {
	plans, err := s.st.ListPlans(r.Context(), 100)
	if err != nil {
		writeError(w, http.StatusInternalServerError, model.ReasonInternal, err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"plans": plans})
}

func (s *Server) handleGetPlan(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/plans/")
	if id == "" {
		writeError(w, http.StatusBadRequest, model.ReasonInternal, "plan id required", nil)
		return
	}
	rec, err := s.st.GetPlan(r.Context(), id)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, model.ReasonInternal, "plan not found", nil)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, model.ReasonInternal, err.Error(), nil)
		return
	}
	writeRawJSON(w, http.StatusOK, rec)
}

// --- Reconcile --------------------------------------------------------------

func (s *Server) handleReconcile(w http.ResponseWriter, r *http.Request) {
	if s.rec == nil {
		writeError(w, http.StatusServiceUnavailable, model.ReasonInternal,
			"reconciler not configured", nil)
		return
	}
	var body struct {
		ClusterID string `json:"cluster_id"`
	}
	_ = decodeStrict(r, &body) // body is optional
	cid := body.ClusterID
	if cid == "" {
		cid = s.clusterID
	}
	res, err := s.rec.Tick(r.Context(), cid, reqID(r))
	if err != nil {
		writeError(w, http.StatusInternalServerError, model.ReasonInternal,
			res.Status+": "+res.Detail, nil)
		return
	}
	code := http.StatusOK
	if res.Status == "failed" {
		code = http.StatusInternalServerError
	} else if res.Status == "degraded" {
		code = http.StatusConflict
	}
	writeJSON(w, code, res)
}

// --- helpers ----------------------------------------------------------------

func decodeStrict(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	// Reject trailing data.
	if dec.More() {
		return errors.New("unexpected trailing JSON content")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeRawJSON(w http.ResponseWriter, code int, m map[string]json.RawMessage) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(m)
}

func writeError(w http.ResponseWriter, code int, kind model.ReasonKind, msg string, instances []model.InstanceFailure) {
	writeJSON(w, code, map[string]any{
		"error": map[string]any{
			"code": string(kind), "message": msg, "instances": instances,
		},
	})
}

func writeFailure(w http.ResponseWriter, code int, planID string, f *model.PlanFailure, steps []scheduler.Step) {
	writeJSON(w, code, map[string]any{
		"plan_id": planID,
		"status":  "failed",
		"error":   f,
		"steps":   steps,
	})
}

// Shutdown gracefully terminates the server.
func Shutdown(srv *http.Server, timeout string) error {
	d, err := time.ParseDuration(timeout)
	if err != nil || d <= 0 {
		d = 10 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), d)
	defer cancel()
	return srv.Shutdown(ctx)
}

// BuildOptions carries wiring config for the command entry point.
type BuildOptions struct {
	Cfg       config.Config
}
