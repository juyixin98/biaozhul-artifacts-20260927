// Package httpapi exposes the planner over HTTP using only the standard
// library. Every error response uses the typed category contract.
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"

	"infraplanner/internal/model"
	"infraplanner/internal/provider"
	"infraplanner/internal/reconciler"
)

// Server wires the reconciler and simulated provider admin endpoints.
type Server struct {
	Rec *reconciler.Service
	Sim *provider.Sim
	Mux *http.ServeMux
}

// New builds the routed server.
func New(rec *reconciler.Service, sim *provider.Sim) *Server {
	s := &Server{Rec: rec, Sim: sim, Mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) routes() {
	s.Mux.HandleFunc("GET /healthz", s.health)
	s.Mux.HandleFunc("POST /v1/plans", s.plan)
	s.Mux.HandleFunc("POST /v1/runs/{id}/apply", s.apply)
	s.Mux.HandleFunc("POST /v1/runs/{id}/resume", s.resume)
	s.Mux.HandleFunc("GET /v1/runs/{id}", s.getRun)
	s.Mux.HandleFunc("GET /v1/runs", s.listRuns)
	s.Mux.HandleFunc("GET /v1/runs/{id}/evidence", s.evidence)
	s.Mux.HandleFunc("GET /v1/live", s.live)

	// simulated-environment administration
	s.Mux.HandleFunc("POST /v1/admin/faults", s.armFault)
	s.Mux.HandleFunc("GET /v1/admin/faults", s.listFaults)
	s.Mux.HandleFunc("POST /v1/admin/reset", s.reset)
	s.Mux.HandleFunc("POST /v1/admin/seed", s.seed)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) plan(w http.ResponseWriter, r *http.Request) {
	var req reconciler.PlanRequest
	if err := decode(r, &req); err != nil {
		writeModelError(w, err)
		return
	}
	resp, err := s.Rec.Plan(r.Context(), req)
	if err != nil {
		writeModelError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) apply(w http.ResponseWriter, r *http.Request) {
	resp, err := s.Rec.Apply(r.Context(), r.PathValue("id"))
	if err != nil {
		writeModelError(w, err)
		return
	}
	writeJSON(w, applyStatus(resp), resp)
}

func (s *Server) resume(w http.ResponseWriter, r *http.Request) {
	resp, err := s.Rec.Resume(r.Context(), r.PathValue("id"))
	if err != nil {
		writeModelError(w, err)
		return
	}
	writeJSON(w, applyStatus(resp), resp)
}

// applyStatus maps a finished (or failed/interrupted) apply response to an
// HTTP status using the typed failure category, so exhaustion (507) is not
// conflated with a state conflict (409).
func applyStatus(resp *reconciler.ApplyResponse) int {
	if resp.State == "succeeded" {
		return http.StatusOK
	}
	if resp.Error != nil {
		switch resp.Error.Category {
		case model.CatInput:
			return http.StatusBadRequest
		case model.CatConflict:
			return http.StatusConflict
		case model.CatExhaustion:
			return http.StatusInsufficientStorage
		case model.CatCompute:
			return http.StatusBadGateway
		}
	}
	return http.StatusConflict
}

func (s *Server) getRun(w http.ResponseWriter, r *http.Request) {
	run, err := s.Rec.GetRun(r.Context(), r.PathValue("id"))
	if err != nil {
		writeModelError(w, err)
		return
	}
	if run == nil {
		writeModelError(w, model.E(model.CatInput, "unknown_run", "no such run"))
		return
	}
	writeJSON(w, http.StatusOK, run)
}

func (s *Server) listRuns(w http.ResponseWriter, r *http.Request) {
	runs, err := s.Rec.ListRuns(r.Context(), 50)
	if err != nil {
		writeModelError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": runs})
}

func (s *Server) evidence(w http.ResponseWriter, r *http.Request) {
	ev, err := s.Rec.Evidence(r.Context(), r.PathValue("id"))
	if err != nil {
		writeModelError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"evidence": ev})
}

func (s *Server) live(w http.ResponseWriter, r *http.Request) {
	obs, err := s.Sim.Observe(r.Context())
	if err != nil {
		writeModelError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, obs)
}

type armFaultReq struct {
	Kind      string    `json:"kind"`
	Target    model.Key `json:"target"`
	Remaining int       `json:"remaining"`
}

func (s *Server) armFault(w http.ResponseWriter, r *http.Request) {
	var req armFaultReq
	if err := decode(r, &req); err != nil {
		writeModelError(w, err)
		return
	}
	s.Sim.ArmFault(provider.Fault{
		Kind: req.Kind, Target: req.Target, Remaining: req.Remaining,
	})
	writeJSON(w, http.StatusOK, map[string]string{"armed": req.Kind})
}

func (s *Server) listFaults(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"faults": s.Sim.Faults()})
}

func (s *Server) reset(w http.ResponseWriter, r *http.Request) {
	s.Sim.Reset()
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}

type seedReq struct {
	Resources []model.Live `json:"resources"`
}

func (s *Server) seed(w http.ResponseWriter, r *http.Request) {
	var req seedReq
	if err := decode(r, &req); err != nil {
		writeModelError(w, err)
		return
	}
	for _, l := range req.Resources {
		s.Sim.Seed(l)
	}
	writeJSON(w, http.StatusOK, map[string]int{"seeded": len(req.Resources)})
}

// ---- helpers ----

func decode(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return model.E(model.CatInput, "bad_json", "invalid request body: %v", err)
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeModelError(w http.ResponseWriter, err error) {
	me, ok := model.AsError(err)
	if !ok {
		// json stream errors etc.
		if errors.Is(err, http.ErrMissingBoundary) {
			// no-op
		}
		me = model.E(model.CatCompute, "internal", "%v", err)
	}
	status := http.StatusInternalServerError
	switch me.Category {
	case model.CatInput:
		status = http.StatusBadRequest
	case model.CatConflict:
		status = http.StatusConflict
	case model.CatExhaustion:
		status = http.StatusInsufficientStorage
	case model.CatCompute:
		status = http.StatusBadGateway
	}
	writeJSON(w, status, map[string]any{
		"error": map[string]string{
			"category": me.Category, "code": me.Code, "message": me.Message,
		},
	})
}
