package api

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strconv"

	"rollctl/internal/adapter"
	"rollctl/internal/controller"
	"rollctl/internal/model"
)

func newReqID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "req-" + hex.EncodeToString(b[:])
}

func (s *Server) routes() {
	m := s.mux
	m.HandleFunc("GET /healthz", s.healthz)

	m.HandleFunc("POST /api/v1/workloads", s.createWorkload)
	m.HandleFunc("GET /api/v1/workloads", s.listWorkloads)
	m.HandleFunc("GET /api/v1/workloads/{name}", s.getWorkload)
	m.HandleFunc("GET /api/v1/workloads/{name}/instances", s.listInstances)
	m.HandleFunc("GET /api/v1/workloads/{name}/events", s.listEvents)

	m.HandleFunc("POST /api/v1/workloads/{name}/releases", s.createRelease)
	m.HandleFunc("GET /api/v1/workloads/{name}/releases", s.listReleases)
	m.HandleFunc("GET /api/v1/releases/{id}", s.getRelease)
	m.HandleFunc("POST /api/v1/workloads/{name}/rollback", s.rollback)

	m.HandleFunc("POST /admin/tick", s.adminTick)
	m.HandleFunc("GET /admin/simulator/capacity", s.getCapacity)
	m.HandleFunc("PUT /admin/simulator/capacity", s.setCapacity)
	m.HandleFunc("PUT /admin/simulator/workloads/{name}/revisions/{revision}/behavior", s.setBehavior)
}

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"ok": true, "tick": s.ctl.CurrentTick()})
}

// policyInput is the JSON form of optional policy overrides.
type policyInput struct {
	MaxSurge            *int   `json:"maxSurge,omitempty"`
	MaxUnavailable      *int   `json:"maxUnavailable,omitempty"`
	ReadyThresholdTicks *int   `json:"readyThresholdTicks,omitempty"`
	DeadlineTicks       *int64 `json:"deadlineTicks,omitempty"`
	MaxStartFailures    *int   `json:"maxStartFailures,omitempty"`
}

func (p policyInput) toModel() *model.Policy {
	pi := &controller.PolicyInput{
		MaxSurge: p.MaxSurge, MaxUnavailable: p.MaxUnavailable,
		ReadyThresholdTicks: p.ReadyThresholdTicks, DeadlineTicks: p.DeadlineTicks,
		MaxStartFailures: p.MaxStartFailures,
	}
	pol := pi.Resolve()
	return &pol
}

// hasPolicy reports whether the JSON carried any policy key.
func (p policyInput) present() bool {
	return p.MaxSurge != nil || p.MaxUnavailable != nil || p.ReadyThresholdTicks != nil ||
		p.DeadlineTicks != nil || p.MaxStartFailures != nil
}

func (s *Server) createWorkload(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name     string      `json:"name"`
		Replicas int         `json:"replicas"`
		Revision string      `json:"revision"`
		Policy   policyInput `json:"policy"`
	}
	if !decode(w, r, &body) {
		return
	}
	var pol model.Policy
	if body.Policy.present() {
		pol = *body.Policy.toModel()
	}
	wl, err := s.ctl.CreateWorkload(r.Context(), controller.CreateWorkloadInput{
		Name: body.Name, Replicas: body.Replicas, Revision: body.Revision,
		Policy: pol, RequestID: reqIDFromCtx(r.Context()),
	})
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, wl)
}

func (s *Server) listWorkloads(w http.ResponseWriter, r *http.Request) {
	// Reuse status per name is not needed here; return names via health-like
	// listing by reading each workload status.
	names := []string{}
	// Controller exposes no list helper directly; status endpoint handles one.
	// Use the releases listing route's backing by trying known names is wrong;
	// instead expose a dedicated passthrough through ListWorkloads.
	got, err := s.ctl.ListWorkloads(r.Context())
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	names = got
	writeJSON(w, 200, map[string]any{"workloads": names})
}

func (s *Server) getWorkload(w http.ResponseWriter, r *http.Request) {
	st, err := s.ctl.Status(r.Context(), r.PathValue("name"))
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	writeJSON(w, 200, st)
}

func (s *Server) listInstances(w http.ResponseWriter, r *http.Request) {
	st, err := s.ctl.Status(r.Context(), r.PathValue("name"))
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	writeJSON(w, 200, map[string]any{"instances": st.Instances})
}

func (s *Server) listEvents(w http.ResponseWriter, r *http.Request) {
	after := int64(0)
	if v := r.URL.Query().Get("afterSeq"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			writeError(w, r, 400, "bad_request", "afterSeq must be an integer")
			return
		}
		after = n
	}
	evs, err := s.ctl.Events(r.Context(), r.PathValue("name"), after)
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	writeJSON(w, 200, map[string]any{"events": evs})
}

func (s *Server) createRelease(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Revision string      `json:"revision"`
		Policy   policyInput `json:"policy"`
	}
	if !decode(w, r, &body) {
		return
	}
	in := controller.CreateReleaseInput{
		Workload: r.PathValue("name"), Revision: body.Revision,
		RequestID: reqIDFromCtx(r.Context()),
	}
	if body.Policy.present() {
		in.Policy = body.Policy.toModel()
	}
	rel, err := s.ctl.CreateRelease(r.Context(), in)
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, rel)
}

func (s *Server) listReleases(w http.ResponseWriter, r *http.Request) {
	rels, err := s.ctl.Releases(r.Context(), r.PathValue("name"))
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	writeJSON(w, 200, map[string]any{"releases": rels})
}

func (s *Server) getRelease(w http.ResponseWriter, r *http.Request) {
	rel, err := s.ctl.Release(r.Context(), r.PathValue("id"))
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	writeJSON(w, 200, rel)
}

func (s *Server) rollback(w http.ResponseWriter, r *http.Request) {
	var body struct {
		TargetRevision string      `json:"targetRevision"`
		Policy         policyInput `json:"policy"`
	}
	if r.ContentLength != 0 {
		if !decode(w, r, &body) {
			return
		}
	}
	in := controller.RollbackInput{
		Workload: r.PathValue("name"), TargetRevision: body.TargetRevision,
		RequestID: reqIDFromCtx(r.Context()),
	}
	if body.Policy.present() {
		in.Policy = body.Policy.toModel()
	}
	rel, err := s.ctl.Rollback(r.Context(), in)
	if err != nil {
		mapControllerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, rel)
}

// adminTick performs exactly one reconcile step.
func (s *Server) adminTick(w http.ResponseWriter, r *http.Request) {
	res, err := s.ctl.Tick(r.Context())
	if err != nil {
		writeError(w, r, 500, "tick_failed", err.Error())
		return
	}
	writeJSON(w, 200, res)
}

func (s *Server) getCapacity(w http.ResponseWriter, r *http.Request) {
	if s.sim == nil {
		writeError(w, r, 503, "simulator_unavailable", "simulator not configured")
		return
	}
	writeJSON(w, 200, map[string]any{"capacity": s.sim.Capacity(), "live": s.sim.LiveCount()})
}

func (s *Server) setCapacity(w http.ResponseWriter, r *http.Request) {
	if s.sim == nil {
		writeError(w, r, 503, "simulator_unavailable", "simulator not configured")
		return
	}
	var body struct {
		Capacity int `json:"capacity"`
	}
	if !decode(w, r, &body) {
		return
	}
	if body.Capacity < 0 {
		writeError(w, r, 400, "bad_request", "capacity must be non-negative")
		return
	}
	if err := s.sim.SetCapacity(r.Context(), body.Capacity); err != nil {
		writeError(w, r, 500, "internal_error", err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"capacity": body.Capacity})
}

func (s *Server) setBehavior(w http.ResponseWriter, r *http.Request) {
	if s.sim == nil {
		writeError(w, r, 503, "simulator_unavailable", "simulator not configured")
		return
	}
	var b adapter.Behavior
	if !decode(w, r, &b) {
		return
	}
	if err := s.sim.SetBehavior(r.Context(), r.PathValue("name"), r.PathValue("revision"), b); err != nil {
		writeError(w, r, 400, "bad_behavior", err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

var _ = model.RelPending
