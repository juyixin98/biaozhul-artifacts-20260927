// Package httpapi exposes the admission pipeline over the standard library
// net/http server. The wire shape mirrors the Kubernetes AdmissionReview
// request/response closely enough to be familiar, while remaining local and
// synthetic.
package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"admission/internal/admission"
	"admission/internal/reconcile"
	"admission/internal/service"
	"admission/internal/types"
)

// AdmissionRequest is the HTTP request body.
type AdmissionRequest struct {
	APIVersion string       `json:"apiVersion"` // admission.example.com/v1
	Kind       string       `json:"kind"`       // AdmissionReview
	Request    RequestInner `json:"request"`
}

// RequestInner is the review payload.
type RequestInner struct {
	UID       string        `json:"uid"`
	Operation string        `json:"operation"`
	Object    types.Object  `json:"object"`
	OldObject *types.Object `json:"oldObject,omitempty"`
	DryRun    bool          `json:"dryRun,omitempty"`
}

// AdmissionResponse is the HTTP response body.
type AdmissionResponse struct {
	APIVersion string        `json:"apiVersion"`
	Kind       string        `json:"kind"`
	Response   ResponseInner `json:"response"`
}

// ResponseInner wraps the core response with a transport-level status.
type ResponseInner struct {
	types.Response
	HTTPStatus int `json:"httpStatus"`
}

// AuditReader reads recent audit events.
type AuditReader func(ctx context.Context, limit int) ([]types.AuditEvent, error)

// QueueCounter counts pending retries.
type QueueCounter func(ctx context.Context) (int, error)

// Server bundles dependencies.
type Server struct {
	svc      *service.Service
	rec      *reconcile.Reconciler
	logger   admission.RunLogger
	newRunID func() string
	audit    AuditReader
	queue    QueueCounter

	mu  sync.Mutex
	seq int
}

// NewServer constructs the HTTP server wrapper.
func NewServer(svc *service.Service, rec *reconcile.Reconciler, logger admission.RunLogger) *Server {
	s := &Server{svc: svc, rec: rec, logger: logger}
	s.newRunID = s.generateRunID
	return s
}

// OverrideRunIDGen is used by tests to make run IDs deterministic.
func (s *Server) OverrideRunIDGen(f func() string) { s.newRunID = f }

// WithWires attaches optional inspection dependencies.
func (s *Server) WithWires(audit AuditReader, queue QueueCounter) *Server {
	s.audit = audit
	s.queue = queue
	return s
}

// Handler returns the configured http.Handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.health)
	mux.HandleFunc("/admission", s.admit)
	mux.HandleFunc("/audit/recent", s.recentAudit)
	mux.HandleFunc("/retry/queue", s.queueDepth)
	return mux
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) admit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var ar AdmissionRequest
	if err := json.NewDecoder(r.Body).Decode(&ar); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if ar.Request.UID == "" {
		writeError(w, http.StatusBadRequest, "request.uid is required")
		return
	}
	if ar.Request.Operation != "CREATE" && ar.Request.Operation != "UPDATE" {
		writeError(w, http.StatusBadRequest, "request.operation must be CREATE or UPDATE")
		return
	}
	req := types.Review{
		UID:        ar.Request.UID,
		Operation:  ar.Request.Operation,
		Object:     ar.Request.Object,
		OldObject:  ar.Request.OldObject,
		DryRun:     ar.Request.DryRun,
		ReceivedAt: time.Now(),
	}

	// Run the synchronous first attempt BEFORE touching the retry queue: this
	// ordering removes any race with the background reconciler over who owns
	// attempt 1. Only a retryable failure schedules a backoff'd retry.
	resp := s.svc.Admit(r.Context(), s.newRunID(), req, 1)
	if !resp.Allowed && !resp.Replayed && !resp.DryRun && resp.FailureCategory.Retryable() {
		if err := s.rec.ScheduleRetry(r.Context(), req, 1); err != nil {
			s.logger.Log(admission.LogEntry{
				RunID: resp.RunID, Time: time.Now(), Level: "ERROR",
				Message:  "schedule retry failed: " + err.Error(),
				Category: types.CatComputeFailure,
			})
		}
	}

	writeJSON(w, http.StatusOK, AdmissionResponse{
		APIVersion: "admission.example.com/v1",
		Kind:       "AdmissionReview",
		Response:   ResponseInner{Response: resp, HTTPStatus: http.StatusOK},
	})
}

func (s *Server) recentAudit(w http.ResponseWriter, r *http.Request) {
	if s.audit == nil {
		writeError(w, http.StatusNotImplemented, "audit reader not configured")
		return
	}
	events, err := s.audit(r.Context(), 20)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": events})
}

func (s *Server) queueDepth(w http.ResponseWriter, r *http.Request) {
	if s.queue == nil {
		writeError(w, http.StatusNotImplemented, "queue counter not configured")
		return
	}
	n, err := s.queue(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"pending": n})
}

// generateRunID produces a replay-traceable identifier.
func (s *Server) generateRunID() string {
	s.mu.Lock()
	s.seq++
	n := s.seq
	s.mu.Unlock()
	return "run-" + time.Now().UTC().Format("20060102T150405.000000") + "-" + itoa(n)
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [12]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{
		"apiVersion": "admission.example.com/v1",
		"kind":       "AdmissionError",
		"error":      msg,
	})
}
