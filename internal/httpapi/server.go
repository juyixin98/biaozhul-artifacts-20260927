// Package httpapi exposes the analysis and replay functions over a small
// local HTTP/JSON API. Every response carries the policy version it was
// produced against; every replay carries a request id and a position-by-
// position trace.
package httpapi

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"fwrule/internal/analyzer"
	"fwrule/internal/replay"
	"fwrule/internal/store"
)

// Server wires the store to HTTP handlers.
type Server struct {
	Store *store.Store
	// Now is overridable in tests.
	Now func() time.Time
}

// New creates a Server with dependencies.
func New(st *store.Store) *Server {
	return &Server{Store: st, Now: func() time.Time { return time.Now().UTC() }}
}

// Handler returns the routed http.Handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/v1/policies", s.handlePolicies)
	mux.HandleFunc("/v1/analyze", s.handleAnalyze)
	mux.HandleFunc("/v1/replay", s.handleReplay)
	mux.HandleFunc("/v1/logs", s.handleLogs)
	mux.HandleFunc("/v1/logs/", s.handleLogByID)
	return logRequestID(mux)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

func (s *Server) handleAnalyze(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet, http.MethodPost:
	default:
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use GET or POST")
		return
	}
	ctx := r.Context()
	version, err := s.resolveVersion(r)
	if err != nil {
		respondStoreError(w, err)
		return
	}
	// Fresh analysis from the stored source (never a cached answer, though the
	// report is also persisted for historical inspection).
	pv, err := s.Store.GetVersion(ctx, version)
	if err != nil {
		respondStoreError(w, err)
		return
	}
	eng, err := replay.NewEngine(pv.Source, pv.Version)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "POLICY_INVALID", err.Error())
		return
	}
	rep := analyzer.Analyze(eng.Pol, pv.Version)
	if err := s.Store.SaveReport(ctx, pv.Version, rep); err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": requestIDFromContext(r),
		"version":    pv.Version,
		"policy":     pv.Name,
		"report":     rep,
		"persisted":  true,
	})
}

func (s *Server) handlePolicies(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	switch r.Method {
	case http.MethodGet:
		vers, err := s.Store.ListVersions(ctx, 100)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
			return
		}
		out := make([]map[string]any, 0, len(vers))
		for _, v := range vers {
			out = append(out, map[string]any{
				"version": v.Version, "name": v.Name, "created_at": v.CreatedAt,
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"request_id": requestIDFromContext(r), "versions": out,
		})
	case http.MethodPost:
		var body struct {
			Name string          `json:"name"`
			Spec json.RawMessage `json:"spec"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeError(w, http.StatusBadRequest, "INVALID_JSON", err.Error())
			return
		}
		if len(body.Spec) == 0 {
			writeError(w, http.StatusBadRequest, "MISSING_SPEC", "field \"spec\" must contain policy JSON")
			return
		}
		pv, pol, err := s.Store.SavePolicy(ctx, body.Name, body.Spec)
		if err != nil {
			writeError(w, http.StatusBadRequest, "POLICY_INVALID", err.Error())
			return
		}
		rep := analyzer.Analyze(pol, pv.Version)
		if err := s.Store.SaveReport(ctx, pv.Version, rep); err != nil {
			writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{
			"request_id": requestIDFromContext(r),
			"version":    pv.Version, "name": pv.Name,
			"report": rep,
		})
	default:
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use GET or POST")
	}
}

func (s *Server) handleReplay(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use POST")
		return
	}
	ctx := r.Context()
	version, err := s.resolveVersion(r)
	if err != nil {
		respondStoreError(w, err)
		return
	}
	pv, err := s.Store.GetVersion(ctx, version)
	if err != nil {
		respondStoreError(w, err)
		return
	}
	var req replay.Request
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_JSON", err.Error())
		return
	}
	if req.RequestID == "" {
		req.RequestID = requestIDFromContext(r)
	}
	eng, err := replay.NewEngine(pv.Source, pv.Version)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "POLICY_INVALID", err.Error())
		return
	}
	dec := eng.Evaluate(req)
	// Decisions (including input errors) are always logged, so "why did this
	// request fail" is explainable afterwards.
	if err := s.Store.LogDecision(ctx, req, dec); err != nil {
		if errors.Is(err, store.ErrDuplicateRequest) {
			writeError(w, http.StatusConflict, "DUPLICATE_REQUEST_ID", err.Error())
			return
		}
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	status := http.StatusOK
	if dec.Status == replay.StatusError {
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, map[string]any{
		"request_id": req.RequestID, "decision": dec,
	})
}

func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use GET")
		return
	}
	limit := 50
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 || n > 1000 {
			writeError(w, http.StatusBadRequest, "INVALID_LIMIT", "limit must be 1..1000")
			return
		}
		limit = n
	}
	logs, err := s.Store.ListLogs(r.Context(), limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": requestIDFromContext(r), "logs": summarizeLogs(logs),
	})
}

func (s *Server) handleLogByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use GET")
		return
	}
	id := r.URL.Path[len("/v1/logs/"):]
	if id == "" {
		writeError(w, http.StatusBadRequest, "MISSING_REQUEST_ID", "use /v1/logs/{request_id}")
		return
	}
	rec, err := s.Store.GetLog(r.Context(), id)
	if err != nil {
		respondStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": rec.RequestID,
		"version":    rec.Version,
		"policy":     rec.PolicyName,
		"request":    rec.Request,
		"decision":   rec.Decision,
		"created_at": rec.CreatedAt,
	})
}

func (s *Server) resolveVersion(r *http.Request) (int64, error) {
	v := r.URL.Query().Get("version")
	if v == "" || v == "latest" {
		return s.Store.ResolveVersion(r.Context(), 0)
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n <= 0 {
		return 0, fmt.Errorf("bad version %q", v)
	}
	return s.Store.ResolveVersion(r.Context(), n)
}

func summarizeLogs(logs []store.LogRecord) []map[string]any {
	out := make([]map[string]any, 0, len(logs))
	for _, l := range logs {
		out = append(out, map[string]any{
			"request_id": l.RequestID,
			"version":    l.Version,
			"policy":     l.PolicyName,
			"status":     l.Decision.Status,
			"action":     l.Decision.Action,
			"decided_by": l.Decision.DecidedBy,
			"error_code": l.Decision.ErrorCode,
			"created_at": l.CreatedAt,
		})
	}
	return out
}

func respondStoreError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrNoPolicy):
		writeError(w, http.StatusNotFound, "NO_POLICY", err.Error())
	case errors.Is(err, store.ErrVersionNotFound):
		writeError(w, http.StatusNotFound, "VERSION_NOT_FOUND", err.Error())
	case errors.Is(err, store.ErrLogNotFound):
		writeError(w, http.StatusNotFound, "LOG_NOT_FOUND", err.Error())
	default:
		writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, code int, errCode, detail string) {
	writeJSON(w, code, map[string]any{
		"error": map[string]string{"code": errCode, "detail": detail},
	})
}
