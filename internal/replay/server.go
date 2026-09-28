// Package replay also provides the local HTTP surface for offline
// replay. Every handler is bound to localhost and performs no outbound
// network I/O: inputs are PCAP bytes or JSON fragments, outputs are
// reports read back from SQLite.
package replay

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"time"

	"ipreasm/internal/config"
	"ipreasm/internal/reasm"
	"ipreasm/internal/store"
)

// Server exposes the offline replay HTTP API.
type Server struct {
	cfg   config.Config
	store *store.SQLiteStore
}

var runIDRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`)

// NewServer builds the HTTP handler.
func NewServer(cfg config.Config, st *store.SQLiteStore) *Server {
	return &Server{cfg: cfg, store: st}
}

// Routes returns the handler tree.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /v1/replay/pcap", s.handlePCAP)
	mux.HandleFunc("POST /v1/replay/fragments", s.handleFragments)
	mux.HandleFunc("GET /v1/runs/{run_id}/datagrams", s.handleDatagrams)
	mux.HandleFunc("GET /v1/runs/{run_id}/events", s.handleEvents)
	mux.HandleFunc("GET /v1/stats", s.handleStats)
	return logRequests(mux)
}

func (s *Server) engine(runID string) *Engine {
	return &Engine{
		RunID: runID,
		Sink:  s.store,
		Cfg: reasm.Config{
			Timeout:          s.cfg.Timeout.Duration,
			MaxDatagramSize:  s.cfg.MaxDatagramSize,
			MaxDatagrams:     s.cfg.MaxDatagrams,
			MaxBufferedBytes: s.cfg.MaxBufferedBytes,
		},
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"time":   time.Now().UTC().Format(time.RFC3339Nano),
		"db":     s.cfg.DBPath,
	})
}

func (s *Server) handlePCAP(w http.ResponseWriter, r *http.Request) {
	runID := s.runID(w, r)
	if runID == "" {
		return
	}
	// Limit body size to 64 MiB for local fixtures.
	body := http.MaxBytesReader(w, r.Body, 64<<20)
	rep, err := s.engine(runID).RunPCAP(body)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_pcap", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (s *Server) handleFragments(w http.ResponseWriter, r *http.Request) {
	runID := s.runID(w, r)
	if runID == "" {
		return
	}
	body := http.MaxBytesReader(w, r.Body, 16<<20)
	raw, err := io.ReadAll(body)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_body", err.Error())
		return
	}
	var req struct {
		Fragments []FragInput `json:"fragments"`
	}
	if err := json.Unmarshal(raw, &req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	if len(req.Fragments) == 0 {
		writeError(w, http.StatusBadRequest, "empty_input", "no fragments supplied")
		return
	}
	rep, err := s.engine(runID).RunFragments(req.Fragments)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_fragment", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (s *Server) handleDatagrams(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("run_id")
	if !runIDRe.MatchString(runID) {
		writeError(w, http.StatusBadRequest, "bad_run_id", "invalid run_id")
		return
	}
	dgs, err := s.store.Datagrams(runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": runID, "datagrams": dgs})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("run_id")
	if !runIDRe.MatchString(runID) {
		writeError(w, http.StatusBadRequest, "bad_run_id", "invalid run_id")
		return
	}
	events, err := s.store.Events(runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": runID, "events": events})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	runID := r.URL.Query().Get("run_id")
	if !runIDRe.MatchString(runID) {
		writeError(w, http.StatusBadRequest, "bad_run_id", "invalid run_id")
		return
	}
	frags, err := s.store.FragCount(runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	dgs, err := s.store.Datagrams(runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"run_id":          runID,
		"buffered_rows":   frags,
		"completed_count": len(dgs),
	})
}

func (s *Server) runID(w http.ResponseWriter, r *http.Request) string {
	runID := r.URL.Query().Get("run_id")
	if runID == "" {
		runID = "run-" + time.Now().UTC().Format("20060102T150405.000000000")
	}
	if !runIDRe.MatchString(runID) {
		writeError(w, http.StatusBadRequest, "bad_run_id", fmt.Sprintf("invalid run_id %q", runID))
		return ""
	}
	return runID
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	// Errors are reported explicitly as failures, never folded into a
	// success response.
	writeJSON(w, status, map[string]any{"ok": false, "error_code": code, "error": msg})
}

// logRequests writes one structured access line per request so a replay
// run can be correlated in logs.
func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(sw, r)
		fmt.Printf("%s %s %s -> %d %s\n", start.UTC().Format(time.RFC3339Nano), r.Method, r.URL.RequestURI(), sw.status, time.Since(start))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}
