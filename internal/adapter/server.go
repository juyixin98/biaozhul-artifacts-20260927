// Package adapter exposes the offline policy engine over plain net/http.
// No third-party router or framework is used.
package adapter

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"sync"

	"netpolicy/internal/diag"
	"netpolicy/internal/domain"
	"netpolicy/internal/engine"
	"netpolicy/internal/reconcile"
	"netpolicy/internal/store"
)

// Holder is the read-mostly handle on the current evaluated revision. The
// reconcile loop swaps snapshots; request handlers take a read lock and
// evaluate against one immutable engine, which is what guarantees label
// snapshots and policy versions agree within a request.
type Holder struct {
	mu  sync.RWMutex
	eng *engine.Engine
}

// Set replaces the active engine atomically.
func (h *Holder) Set(eng *engine.Engine) {
	h.mu.Lock()
	h.eng = eng
	h.mu.Unlock()
}

// Engine returns the active engine.
func (h *Holder) Engine() *engine.Engine {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.eng
}

// Server bundles dependencies for the HTTP layer.
type Server struct {
	Holder *Holder
	Store  *store.Store
	Loop   *reconcile.Loop
	Logger *slog.Logger
}

// NewRouter wires all routes.
func (s *Server) NewRouter() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /v1/check", s.handleCheck)
	mux.HandleFunc("POST /v1/matrix", s.handleMatrix)
	mux.HandleFunc("GET /v1/status", s.handleStatus)
	mux.HandleFunc("GET /v1/snapshots", s.handleSnapshots)
	mux.HandleFunc("GET /v1/snapshots/{revision}", s.handleSnapshot)
	mux.HandleFunc("POST /internal/refresh", s.handleRefresh)
	return diag.RequestIDMiddleware(mux)
}

type checkRequest struct {
	SourceUID   string `json:"sourceUid"`
	DestUID     string `json:"destUid"`
	Protocol    string `json:"protocol"`
	Port        int    `json:"port"`
	PinRevision int64  `json:"pinRevision,omitempty"`
}

type errorBody struct {
	Error     apiError `json:"error"`
	RequestID string   `json:"requestId"`
}

type apiError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleCheck(w http.ResponseWriter, r *http.Request) {
	var req checkRequest
	if !decode(w, r, s.Logger, &req) {
		return
	}
	eng := s.Holder.Engine()
	if eng == nil {
		writeError(w, r, http.StatusServiceUnavailable, "no_snapshot", "no reconciled snapshot available yet")
		return
	}
	proto, err := domain.ParseProtocol(req.Protocol)
	if err != nil {
		writeError(w, r, http.StatusBadRequest, "invalid_protocol", err.Error())
		return
	}
	if strings.TrimSpace(req.SourceUID) == "" || strings.TrimSpace(req.DestUID) == "" {
		writeError(w, r, http.StatusBadRequest, "missing_endpoint", "sourceUid and destUid are required")
		return
	}
	d, err := eng.Check(engine.Input{
		SourceUID:   req.SourceUID,
		DestUID:     req.DestUID,
		Protocol:    proto,
		Port:        req.Port,
		PinRevision: req.PinRevision,
	})
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "check_failed", err.Error())
		return
	}
	logDecision(r, s.Logger, req.SourceUID, req.DestUID, req.Port, d)
	writeJSON(w, http.StatusOK, d)
}

type matrixRequest struct {
	Protocol    string `json:"protocol"`
	Port        *int   `json:"port,omitempty"`
	PinRevision int64  `json:"pinRevision,omitempty"`
	// AllDeclaredPorts: when true and port is omitted, sweep every
	// (protocol,number) pair declared by any endpoint.
	AllDeclaredPorts bool `json:"allDeclaredPorts,omitempty"`
}

func (s *Server) handleMatrix(w http.ResponseWriter, r *http.Request) {
	var req matrixRequest
	if !decode(w, r, s.Logger, &req) {
		return
	}
	eng := s.Holder.Engine()
	if eng == nil {
		writeError(w, r, http.StatusServiceUnavailable, "no_snapshot", "no reconciled snapshot available yet")
		return
	}
	proto, err := domain.ParseProtocol(req.Protocol)
	if err != nil {
		// For allDeclaredPorts sweeps the protocol is taken from declared
		// ports, so an empty protocol is acceptable there.
		if !(req.AllDeclaredPorts && req.Port == nil && strings.TrimSpace(req.Protocol) == "") {
			writeError(w, r, http.StatusBadRequest, "invalid_protocol", err.Error())
			return
		}
	}
	if req.Port != nil {
		m, err := eng.Matrix(engine.MatrixRequest{Protocol: proto, Port: *req.Port, PinRevision: req.PinRevision})
		if err != nil {
			writeError(w, r, http.StatusInternalServerError, "matrix_failed", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, m)
		return
	}
	if req.AllDeclaredPorts {
		// Build a union sweep, optionally filtered by requested protocol.
		res := &engine.FullMatrixResult{Revision: eng.Revision()}
		for _, ref := range eng.EndpointPorts() {
			if proto != "" && ref.Protocol != proto {
				continue
			}
			m, err := eng.Matrix(engine.MatrixRequest{Protocol: ref.Protocol, Port: ref.Number, PinRevision: req.PinRevision})
			if err != nil {
				writeError(w, r, http.StatusInternalServerError, "matrix_failed", err.Error())
				return
			}
			res.Matrices = append(res.Matrices, m)
		}
		writeJSON(w, http.StatusOK, res)
		return
	}
	writeError(w, r, http.StatusBadRequest, "missing_port", "set port, or allDeclaredPorts=true")
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	resp := map[string]any{}
	if rec, ok := s.Loop.Last(); ok {
		resp["lastRun"] = rec
	} else {
		resp["lastRun"] = nil
	}
	if eng := s.Holder.Engine(); eng != nil {
		resp["revision"] = eng.Revision()
		snap := eng.Snapshot()
		resp["counts"] = map[string]int{
			"namespaces": len(snap.Namespaces),
			"endpoints":  len(snap.Endpoints),
			"policies":   len(snap.Policies),
		}
	} else {
		resp["revision"] = nil
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleSnapshots(w http.ResponseWriter, r *http.Request) {
	revs, err := s.Store.Revisions(r.Context())
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "store_failed", err.Error())
		return
	}
	runs, err := s.Store.RecentRuns(r.Context(), 20)
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "store_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"revisions": revs, "recentRuns": runs})
}

func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	var rev int64
	if _, err := parsePathInt(r.PathValue("revision"), &rev); err != nil {
		writeError(w, r, http.StatusBadRequest, "invalid_revision", err.Error())
		return
	}
	snap, err := s.Store.GetRevision(r.Context(), rev)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, r, http.StatusNotFound, "snapshot_not_found", err.Error())
		return
	}
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "store_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

func (s *Server) handleRefresh(w http.ResponseWriter, r *http.Request) {
	s.Loop.Trigger()
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "triggered"})
}

// LoadLatestEngine swaps the holder to the latest stored snapshot.
func (s *Server) LoadLatestEngine(ctx context.Context) error {
	snap, err := s.Store.Latest(ctx)
	if err != nil {
		return err
	}
	if snap == nil {
		return nil
	}
	s.Holder.Set(engine.New(snap))
	return nil
}
