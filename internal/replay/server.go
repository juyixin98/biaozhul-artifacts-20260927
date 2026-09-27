package replay

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"

	"natlab/internal/config"
	"natlab/internal/model"
	"natlab/internal/nat"
	"natlab/internal/storage"
)

// Server is the local HTTP replay interface. It binds to 127.0.0.1 by default
// and only operates on synthetic metadata; it never forwards packets.
type Server struct {
	cfg   config.Config
	store storage.Store

	mu      sync.Mutex
	engines map[string]*nat.Engine
}

// NewServer creates a replay HTTP server backed by st.
func NewServer(cfg config.Config, st storage.Store) *Server {
	return &Server{cfg: cfg, store: st, engines: map[string]*nat.Engine{}}
}

// Handler wires routes onto a mux so tests can exercise httptest.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/v1/runs", s.handleCreateRun)
	mux.HandleFunc("/v1/runs/", s.handleRunSub)
	return mux
}

// engineFor returns the cached engine for a run, restoring it from the store
// when the process reopened a persistent database.
func (s *Server) engineFor(runID string) (*nat.Engine, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if eng, ok := s.engines[runID]; ok {
		return eng, nil
	}
	if _, err := s.store.GetRun(context.Background(), runID); err != nil {
		return nil, err
	}
	eng, err := nat.New(runID, s.cfg, s.store)
	if err != nil {
		return nil, err
	}
	if err := eng.Restore(context.Background(), s.store, runID); err != nil {
		return nil, err
	}
	s.engines[runID] = eng
	return eng, nil
}

type envelope struct {
	OK    bool      `json:"ok"`
	Error *apiError `json:"error,omitempty"`
	Data  any       `json:"data,omitempty"`
}

type apiError struct {
	Class  model.Class `json:"class"`
	Reason string      `json:"reason"`
	Detail string      `json:"detail"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, envelope{OK: true, Data: map[string]string{"status": "ready"}})
}

type createRunReq struct {
	RunID  string         `json:"run_id"`
	Name   string         `json:"name"`
	Config *config.Config `json:"config,omitempty"`
}

func (s *Server) handleCreateRun(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, envelope{Error: &apiError{
			Reason: "method_not_allowed", Detail: "POST required"}})
		return
	}
	var req createRunReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, envelope{Error: &apiError{
			Class: model.ClassInput, Reason: "malformed_json", Detail: err.Error()}})
		return
	}
	if req.RunID == "" {
		writeJSON(w, http.StatusBadRequest, envelope{Error: &apiError{
			Class: model.ClassInput, Reason: "invalid_input", Detail: "run_id required"}})
		return
	}
	cfg := s.cfg
	if req.Config != nil {
		cfg = *req.Config
	}
	if err := cfg.Validate(); err != nil {
		writeJSON(w, http.StatusBadRequest, envelope{Error: &apiError{
			Class: model.ClassInput, Reason: "invalid_config", Detail: err.Error()}})
		return
	}
	s.mu.Lock()
	if _, exists := s.engines[req.RunID]; exists {
		s.mu.Unlock()
		writeJSON(w, http.StatusConflict, envelope{Error: &apiError{
			Reason: "run_exists", Detail: "run already open: " + req.RunID}})
		return
	}
	eng, err := nat.New(req.RunID, cfg, s.store)
	if err != nil {
		s.mu.Unlock()
		writeJSON(w, http.StatusInternalServerError, envelope{Error: &apiError{
			Class: model.ClassCompute, Reason: "compute_failure", Detail: err.Error()}})
		return
	}
	s.engines[req.RunID] = eng
	s.mu.Unlock()
	writeJSON(w, http.StatusCreated, envelope{OK: true, Data: map[string]any{
		"run_id": req.RunID, "name": req.Name}})
}

func (s *Server) handleRunSub(w http.ResponseWriter, r *http.Request) {
	// /v1/runs/{id}/packets|decisions|mappings|stats
	path := r.URL.Path
	base := "/v1/runs/"
	rest := path[len(base):]
	var runID, sub string
	for i := 0; i < len(rest); i++ {
		if rest[i] == '/' {
			runID, sub = rest[:i], rest[i+1:]
			break
		}
	}
	if runID == "" {
		writeJSON(w, http.StatusNotFound, envelope{Error: &apiError{Reason: "not_found"}})
		return
	}
	eng, err := s.engineFor(runID)
	if err != nil {
		writeJSON(w, http.StatusNotFound, envelope{Error: &apiError{
			Reason: "run_not_found", Detail: err.Error()}})
		return
	}

	switch {
	case sub == "packets" && r.Method == http.MethodPost:
		var p model.Packet
		if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
			writeJSON(w, http.StatusBadRequest, envelope{Error: &apiError{
				Class: model.ClassInput, Reason: "malformed_json", Detail: err.Error()}})
			return
		}
		res, perr := eng.Process(r.Context(), p)
		if perr != nil {
			writeJSON(w, http.StatusInternalServerError, envelope{OK: false, Error: &apiError{
				Class: model.ClassCompute, Reason: "compute_failure",
				Detail: perr.Error()}, Data: map[string]any{"decision": res.Decision}})
			return
		}
		writeJSON(w, http.StatusOK, envelope{OK: true, Data: map[string]any{
			"decision": res.Decision,
			"expired":  res.Expired,
		}})

	case sub == "decisions" && r.Method == http.MethodGet:
		from := int64(0)
		limit := 0
		if v := r.URL.Query().Get("from_seq"); v != "" {
			fmt.Sscanf(v, "%d", &from)
		}
		if v := r.URL.Query().Get("limit"); v != "" {
			fmt.Sscanf(v, "%d", &limit)
		}
		ds, err := s.store.ListDecisions(r.Context(), runID, from, limit)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, envelope{Error: &apiError{
				Class: model.ClassCompute, Detail: err.Error()}})
			return
		}
		writeJSON(w, http.StatusOK, envelope{OK: true, Data: map[string]any{"decisions": ds}})

	case sub == "mappings" && r.Method == http.MethodGet:
		writeJSON(w, http.StatusOK, envelope{OK: true,
			Data: map[string]any{"mappings": eng.Snapshots()}})

	case sub == "stats" && r.Method == http.MethodGet:
		writeJSON(w, http.StatusOK, envelope{OK: true, Data: eng.Stats()})

	default:
		writeJSON(w, http.StatusNotFound, envelope{Error: &apiError{
			Reason: "not_found", Detail: "unknown sub-resource: " + sub}})
	}
}
