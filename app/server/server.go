// Package server wires the controller, the durable local fixture and SQLite
// stores to a standard-library HTTP API. Every response is tied to a request
// id (client-supplied via X-Request-ID or generated), and every tick returns
// the full, explainable decision document.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"sync/atomic"
	"time"

	"replicactl/app/store"
	"replicactl/core/controller"
	"replicactl/core/model"
)

// Server holds the wired components.
type Server struct {
	Controller *controller.Controller
	Fixture    *store.DurableFixture
	Decisions  controller.DecisionStore
	History    controller.RawHistory
	// RawDecisions powers the read-only query endpoints regardless of any
	// fault-injecting wrapper installed around Decisions.
	RawDecisions *store.DecisionLog
	Logger       *log.Logger
	Clock        func() int64

	mux     *http.ServeMux
	httpSrv *http.Server
	seq     atomic.Uint64

	// Optional fault-injecting wrappers; the admin faults endpoint toggles
	// them when present.
	FaultyDecisions *FaultyDecisionStore
	FaultyHistory   *FaultyHistory
}

// New wires the components. Stores are accepted as interfaces so tests can
// install fault-injecting wrappers.
func New(ctl *controller.Controller, fx *store.DurableFixture, decisions controller.DecisionStore, history controller.RawHistory, logger *log.Logger) *Server {
	s := &Server{
		Controller: ctl,
		Fixture:    fx,
		Decisions:  decisions,
		History:    history,
		Logger:     logger,
		Clock:      func() int64 { return time.Now().Unix() },
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/metrics", s.handleMetric)
	mux.HandleFunc("POST /v1/demand", s.handleDemand)
	mux.HandleFunc("POST /v1/reconcile", s.handleReconcile)
	mux.HandleFunc("GET /v1/decisions", s.handleDecisions)
	mux.HandleFunc("GET /v1/requests/{request_id}", s.handleRequestLookup)
	mux.HandleFunc("GET /v1/fixture", s.handleFixture)
	mux.HandleFunc("POST /v1/admin/faults", s.handleFaults)
	mux.HandleFunc("POST /v1/admin/seed", s.handleSeed)
	mux.HandleFunc("GET /healthz", s.handleHealth)
	s.mux = mux
	return s
}

// Handler returns the wrapped HTTP handler (exposed for tests).
func (s *Server) Handler() http.Handler { return s.withRequestID(s.mux) }

// ListenAndServe starts the HTTP listener.
func (s *Server) ListenAndServe(addr string) error {
	s.httpSrv = &http.Server{
		Addr:              addr,
		Handler:           s.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	s.Logger.Printf("component=http_server location=%s msg=listening version=schema%d", addr, 1)
	return s.httpSrv.ListenAndServe()
}

// Shutdown gracefully stops the server.
func (s *Server) Shutdown(ctx context.Context) error {
	if s.httpSrv == nil {
		return nil
	}
	return s.httpSrv.Shutdown(ctx)
}

type ctxKey string

const requestIDKey ctxKey = "request_id"

func (s *Server) withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rid := r.Header.Get("X-Request-ID")
		if rid == "" {
			rid = fmt.Sprintf("req-%d-%06d", start.UnixNano(), s.seq.Add(1))
		}
		r = r.WithContext(context.WithValue(r.Context(), requestIDKey, rid))
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)
		s.Logger.Printf("request_id=%s method=%s path=%s status=%d duration_ms=%d",
			rid, r.Method, r.URL.Path, sw.status, time.Since(start).Milliseconds())
	})
}

func requestID(r *http.Request) string {
	if v, ok := r.Context().Value(requestIDKey).(string); ok {
		return v
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

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// errBody is the single error envelope. Category holds the machine-readable
// failure class when the controller produced one.
type errBody struct {
	Error     string `json:"error"`
	RequestID string `json:"request_id"`
	Category  string `json:"category,omitempty"`
	Uncertain bool   `json:"uncertain,omitempty"`
}

func writeError(w http.ResponseWriter, r *http.Request, code int, category, msg string) {
	writeJSON(w, code, errBody{
		Error:     msg,
		RequestID: requestID(r),
		Category:  category,
	})
}

type metricReq struct {
	InstanceID string  `json:"instance_id"`
	Load       float64 `json:"load"`
	ReportedAt int64   `json:"reported_at"`
}

func (s *Server) handleMetric(w http.ResponseWriter, r *http.Request) {
	var req metricReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), "invalid JSON body: "+err.Error())
		return
	}
	sample := model.LoadSample{InstanceID: req.InstanceID, Load: req.Load, ReportedAt: req.ReportedAt}
	if err := s.Fixture.SubmitSample(sample); err != nil {
		writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), err.Error())
		return
	}
	s.Logger.Printf("request_id=%s step=metric_accepted instance=%s load=%v reported_at=%d",
		requestID(r), req.InstanceID, req.Load, req.ReportedAt)
	writeJSON(w, http.StatusAccepted, map[string]any{
		"status": "accepted", "request_id": requestID(r),
	})
}

type demandReq struct {
	Present    bool  `json:"present"`
	ReportedAt int64 `json:"reported_at"`
}

func (s *Server) handleDemand(w http.ResponseWriter, r *http.Request) {
	var req demandReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), "invalid JSON body: "+err.Error())
		return
	}
	if req.ReportedAt <= 0 {
		writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), "reported_at must be a positive unix timestamp")
		return
	}
	if err := s.Fixture.PostDemand(req.Present, req.ReportedAt); err != nil {
		writeError(w, r, http.StatusInternalServerError, string(controller.FailureStore), err.Error())
		return
	}
	s.Logger.Printf("request_id=%s step=demand_recorded present=%v reported_at=%d",
		requestID(r), req.Present, req.ReportedAt)
	writeJSON(w, http.StatusAccepted, map[string]any{
		"status": "accepted", "request_id": requestID(r),
	})
}

type reconcileReq struct {
	At *int64 `json:"at"`
}

func (s *Server) handleReconcile(w http.ResponseWriter, r *http.Request) {
	var req reconcileReq
	if r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil && !errors.Is(err, io.EOF) {
			writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), "invalid JSON body: "+err.Error())
			return
		}
	}
	at := s.Clock()
	if req.At != nil {
		at = *req.At
	}
	if at <= 0 {
		writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), "at must be a positive unix timestamp")
		return
	}
	rid := requestID(r)
	s.Logger.Printf("request_id=%s step=reconcile_start tick_at=%d location=controller.Reconcile", rid, at)
	dec, err := s.Controller.Reconcile(at, rid)
	if err != nil {
		s.Logger.Printf("request_id=%s step=reconcile_failed class=%s detail=%q", rid, dec.FailureClass, dec.FailureDetail)
		writeError(w, r, http.StatusConflict, string(dec.FailureClass),
			fmt.Sprintf("reconcile failed: %s", dec.FailureDetail))
		return
	}
	s.Logger.Printf("request_id=%s step=reconcile_done action=%s current=%d desired=%d reasons=%v",
		rid, dec.Action, dec.CurrentReplicas, dec.DesiredReplicas, dec.Reasons)
	writeJSON(w, http.StatusOK, dec)
}

func (s *Server) decisionLog() (*store.DecisionLog, bool) {
	if s.RawDecisions != nil {
		return s.RawDecisions, true
	}
	d, ok := s.Decisions.(*store.DecisionLog)
	return d, ok
}

func (s *Server) handleDecisions(w http.ResponseWriter, r *http.Request) {
	limit := 20
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 || n > 500 {
			writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), "limit must be in [1,500]")
			return
		}
		limit = n
	}
	dl, ok := s.decisionLog()
	if !ok {
		writeError(w, r, http.StatusInternalServerError, string(controller.FailureStore), "decision log unavailable")
		return
	}
	ds, err := dl.Recent(limit)
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, string(controller.FailureStore), err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"decisions": ds, "count": len(ds)})
}

func (s *Server) handleRequestLookup(w http.ResponseWriter, r *http.Request) {
	rid := r.PathValue("request_id")
	dl, ok := s.decisionLog()
	if !ok {
		writeError(w, r, http.StatusInternalServerError, string(controller.FailureStore), "decision log unavailable")
		return
	}
	d, ok, err := dl.ByRequestID(rid)
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, string(controller.FailureStore), err.Error())
		return
	}
	if !ok {
		writeError(w, r, http.StatusNotFound, "REQUEST_NOT_FOUND", "no decision for request id "+rid)
		return
	}
	writeJSON(w, http.StatusOK, d)
}

func (s *Server) handleFixture(w http.ResponseWriter, r *http.Request) {
	cur, err := s.Fixture.CurrentReplicas()
	if err != nil {
		writeError(w, r, http.StatusConflict, string(controller.FailureFleetRead), err.Error())
		return
	}
	samples, err := s.Fixture.LatestSamples(s.Clock())
	if err != nil {
		writeError(w, r, http.StatusConflict, string(controller.FailureMetricRead), err.Error())
		return
	}
	demand, hasDemand, err := s.Fixture.LatestDemand(s.Clock())
	if err != nil {
		writeError(w, r, http.StatusConflict, string(controller.FailureMetricRead), err.Error())
		return
	}
	view := map[string]any{
		"request_id":       requestID(r),
		"current_replicas": cur,
		"instances":        samples,
		"demand_present":   hasDemand && demand.Present,
		"demand_reported_at": func() int64 {
			if hasDemand {
				return demand.ReportedAt
			}
			return 0
		}(),
	}
	writeJSON(w, http.StatusOK, view)
}

type faultsReq struct {
	SetReplicas    *string `json:"set_replicas"`
	CurrentRead    *string `json:"current_read"`
	MetricRead     *string `json:"metric_read"`
	DemandRead     *string `json:"demand_read"`
	DecisionAppend *string `json:"decision_append"`
	HistoryRead    *string `json:"history_read"`
}

// handleFaults injects or clears synthetic dependency faults. A null field or
// empty string clears that hook; a non-empty string is the error the next
// dependency call returns. This endpoint exists for the failure-category tests
// and only drives the local fixture.
func (s *Server) handleFaults(w http.ResponseWriter, r *http.Request) {
	var req faultsReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), "invalid JSON body: "+err.Error())
		return
	}
	apply := func(p *string, set func(error)) {
		if p == nil || *p == "" {
			set(nil)
			return
		}
		set(errors.New(*p))
	}
	apply(req.SetReplicas, func(e error) { s.Fixture.FailSetReplicas = e })
	apply(req.CurrentRead, func(e error) { s.Fixture.FailCurrentRead = e })
	apply(req.MetricRead, func(e error) { s.Fixture.FailMetricRead = e })
	apply(req.DemandRead, func(e error) { s.Fixture.FailDemandRead = e })
	if s.FaultyDecisions != nil {
		apply(req.DecisionAppend, s.FaultyDecisions.SetFail)
	}
	if s.FaultyHistory != nil {
		apply(req.HistoryRead, func(e error) {
			s.FaultyHistory.SetReadFail(e)
			if e == nil {
				s.FaultyHistory.SetAppendFail(nil)
			}
		})
	}
	s.Logger.Printf("request_id=%s step=faults_reconfigured set=%s current=%s metric=%s demand=%s decision=%s history=%s",
		requestID(r), strPtr(req.SetReplicas), strPtr(req.CurrentRead), strPtr(req.MetricRead),
		strPtr(req.DemandRead), strPtr(req.DecisionAppend), strPtr(req.HistoryRead))
	writeJSON(w, http.StatusOK, map[string]any{"status": "applied", "request_id": requestID(r)})
}

func strPtr(p *string) string {
	if p == nil {
		return "<unset>"
	}
	if *p == "" {
		return "<cleared>"
	}
	return *p
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

type seedReq struct {
	Replicas int `json:"replicas"`
}

// handleSeed sets the initial fleet size for a scenario. This endpoint is a
// local fixture initialiser only; it never participates in a reconcile.
func (s *Server) handleSeed(w http.ResponseWriter, r *http.Request) {
	var req seedReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), "invalid JSON body: "+err.Error())
		return
	}
	if err := s.Fixture.SeedReplicas(req.Replicas); err != nil {
		writeError(w, r, http.StatusBadRequest, string(controller.FailureInvalidInput), err.Error())
		return
	}
	s.Logger.Printf("request_id=%s step=fleet_seeded replicas=%d", requestID(r), req.Replicas)
	writeJSON(w, http.StatusOK, map[string]any{"status": "seeded", "replicas": req.Replicas, "request_id": requestID(r)})
}
