package adapter

import (
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/model"
)

type sampleReq struct {
	Metric     string     `json:"metric"`
	Value      *float64   `json:"value"`
	ObservedAt *time.Time `json:"observed_at"`
}

// handleSample ingests one load report from a synthetic instance. A missing
// observed_at defaults to arrival time; a late observed_at is accepted and
// simply ages out (delayed reporting is a tested behaviour).
func (s *Server) handleSample(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r.Context())
	id := r.PathValue("id")
	if id == "" {
		writeError(w, r, http.StatusBadRequest, "BAD_INSTANCE_ID", "instance id required in path")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<16))
	if err != nil {
		writeError(w, r, http.StatusBadRequest, "BAD_BODY", "cannot read body")
		return
	}
	var req sampleReq
	if err := json.Unmarshal(body, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "BAD_JSON", "invalid JSON: "+err.Error())
		return
	}
	if req.Value == nil {
		writeError(w, r, http.StatusBadRequest, "MISSING_VALUE", "field 'value' is required")
		return
	}
	if *req.Value < 0 {
		writeError(w, r, http.StatusBadRequest, "BAD_VALUE", "field 'value' must be >= 0")
		return
	}
	cfg, _, err := s.store.LoadConfig(r.Context())
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "CONFIG_UNREADABLE", err.Error())
		return
	}
	metric := req.Metric
	if metric == "" {
		metric = cfg.Metric
	}
	now := s.now()
	obs := now
	if req.ObservedAt != nil {
		obs = req.ObservedAt.UTC()
	}
	sm := model.Sample{
		InstanceID: id, Metric: metric, Value: *req.Value,
		ObservedAt: obs, ReceivedAt: now,
	}
	if err := s.store.InsertSample(r.Context(), sm); err != nil {
		writeError(w, r, http.StatusInternalServerError, "INGEST_FAILED", err.Error())
		return
	}
	s.log.Info("sample_ingested",
		"request_id", rid, "instance", id, "metric", metric,
		"value", *req.Value, "observed_age", now.Sub(obs).Truncate(time.Second))
	writeJSON(w, http.StatusAccepted, map[string]any{
		"status": "accepted", "request_id": rid,
		"instance_id": id, "metric": metric, "observed_at": obs,
	})
}

type demandReq struct {
	Pending    *int64     `json:"pending"`
	ObservedAt *time.Time `json:"observed_at"`
}

// handleDemand ingests the external scale-from-zero work signal.
func (s *Server) handleDemand(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r.Context())
	var req demandReq
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
		writeError(w, r, http.StatusBadRequest, "BAD_JSON", "invalid JSON: "+err.Error())
		return
	}
	if req.Pending == nil {
		writeError(w, r, http.StatusBadRequest, "MISSING_PENDING", "field 'pending' is required")
		return
	}
	if *req.Pending < 0 {
		writeError(w, r, http.StatusBadRequest, "BAD_PENDING", "field 'pending' must be >= 0")
		return
	}
	now := s.now()
	obs := now
	if req.ObservedAt != nil {
		obs = req.ObservedAt.UTC()
	}
	d := model.Demand{Pending: *req.Pending, ObservedAt: obs, ReceivedAt: now}
	if err := s.store.InsertDemand(r.Context(), d); err != nil {
		writeError(w, r, http.StatusInternalServerError, "INGEST_FAILED", err.Error())
		return
	}
	s.log.Info("demand_ingested", "request_id", rid, "pending", *req.Pending)
	writeJSON(w, http.StatusAccepted, map[string]any{"status": "accepted", "request_id": rid})
}

// handleReconcile triggers one deterministic tick.
func (s *Server) handleReconcile(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r.Context())
	d, err := s.engine.Reconcile(r.Context(), rid)
	if err != nil {
		s.log.Error("reconcile_failed", "request_id", rid, "error", err)
		writeError(w, r, http.StatusInternalServerError, "RECONCILE_FAILED", err.Error())
		return
	}
	s.LogDecisionFor(d)
	writeJSON(w, http.StatusOK, d)
}

// LogDecisionFor emits the structured, explainable log line for a decision.
func (s *Server) LogDecisionFor(d *model.Decision) { s.logDecision(d) }

func (s *Server) logDecision(d *model.Decision) {
	args := []any{
		"request_id", d.RequestID,
		"tick_at", d.TickAt,
		"action", d.Action,
		"current", d.CurrentReplicas,
		"desired", d.DesiredReplicas,
		"applied", d.AppliedReplicas,
		"config_version", d.ConfigVersion,
		"config_revision", d.ConfigRevision,
		"location", d.Location,
		"fresh", len(d.FreshInstances),
		"stale", len(d.StaleInstances),
		"missing", len(d.MissingInstances),
	}
	for _, rs := range d.Reasons {
		args = append(args, "reason:"+rs.Code, rs.Message)
	}
	for _, u := range d.Uncertainties {
		args = append(args, "uncertainty", u)
	}
	if d.ActuatorError != "" {
		args = append(args, "actuator_error", d.ActuatorError)
	}
	s.log.Info("reconcile_complete", args...)
}

// handleFleet returns the current resource model.
func (s *Server) handleFleet(w http.ResponseWriter, r *http.Request) {
	f, err := s.store.Fleet(r.Context())
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "FLEET_UNREADABLE", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id":     requestID(r.Context()),
		"replicas":       f.Replicas(),
		"instances":      f.Instances,
		"updated_at":     f.UpdatedAt,
		"config_version": config.SchemaVersion,
	})
}

// handleDecisions lists recent explainable decisions.
func (s *Server) handleDecisions(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := strconv.Atoi(l); err == nil && n > 0 && n <= 500 {
			limit = n
		}
	}
	list, err := s.store.Decisions(r.Context(), limit)
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "DECISIONS_UNREADABLE", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"request_id": requestID(r.Context()), "decisions": list})
}

// handleDecision fetches one decision by request id.
func (s *Server) handleDecision(w http.ResponseWriter, r *http.Request) {
	rid := r.PathValue("requestID")
	d, err := s.store.DecisionByRequestID(r.Context(), rid)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, r, http.StatusNotFound, "NOT_FOUND", "no decision for request id: "+rid)
			return
		}
		writeError(w, r, http.StatusInternalServerError, "DECISION_UNREADABLE", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, d)
}
