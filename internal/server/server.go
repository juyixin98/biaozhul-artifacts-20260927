// Package server exposes the state core over HTTP using only the standard
// library. It is a thin, interactive front-end for the same injected-clock
// state machine the replay engine drives; nothing here reads a wall clock.
package server

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"sync/atomic"

	"igmpv2timer/internal/clock"
	"igmpv2timer/internal/config"
	"igmpv2timer/internal/core"
	"igmpv2timer/internal/model"
	"igmpv2timer/internal/store"
)

// Server bundles the HTTP handlers with their dependencies.
type Server struct {
	cfg  config.Config
	clk  *clock.Clock
	core *core.Core
	st   *store.Store
	log  *log.Logger
	req  uint64
	mux  *http.ServeMux
}

// New builds a server. If st is nil, requests are not journaled.
func New(cfg config.Config, clk *clock.Clock, c *core.Core, st *store.Store,
	logger *log.Logger) *Server {
	if logger == nil {
		logger = log.Default()
	}
	s := &Server{cfg: cfg, clk: clk, core: c, st: st, log: logger}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/state", s.handleState)
	mux.HandleFunc("/events", s.handleEvent)
	mux.HandleFunc("/tick", s.handleTick)
	mux.HandleFunc("/diags", s.handleDiags)
	mux.HandleFunc("/intervals", s.handleIntervals)
	s.mux = mux
	return s
}

// Handler returns the root http.Handler (with request-id + recovery wrap).
func (s *Server) Handler() http.Handler {
	return s.withRequestID(s.recoverer(s.mux))
}

// eventRequest is the JSON body for POST /events.
type eventRequest struct {
	AtMs       int64  `json:"at_ms"`
	Kind       string `json:"kind"` // report|leave|general_query
	Iface      string `json:"iface"`
	Group      string `json:"group"`
	Member     string `json:"member"`
	SourceAddr string `json:"source_addr"`
	ResponseTo string `json:"response_to,omitempty"`
	RequestID  string `json:"request_id,omitempty"`
}

type errorBody struct {
	RequestID string `json:"request_id"`
	Error     string `json:"error"`
	Category  string `json:"error_category"`
}

type diagBody struct {
	RequestID string     `json:"request_id"`
	Diag      model.Diag `json:"diag"`
}

func (s *Server) nextRequestID(client string) string {
	n := atomic.AddUint64(&s.req, 1)
	if client != "" {
		return client + "#" + strconv.FormatUint(n, 10)
	}
	return "req-" + strconv.FormatUint(n, 10)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) writeErr(w http.ResponseWriter, status int, reqID, category, msg string) {
	// Diagnostic messages never echo raw source addresses; helpers in
	// model mask them and handlers use synthetic member names.
	s.log.Printf("req=%s status=%d category=%s %s", reqID, status, category, msg)
	writeJSON(w, status, errorBody{RequestID: reqID, Error: msg, Category: category})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	reqID := r.Context().Value(reqIDKey{}).(string)
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": reqID,
		"service":    s.cfg.ServiceName,
		"status":     "ok",
		"now_ms":     int64(s.clk.Now()),
		"scope":      "offline IGMPv2 membership timers; not a multicast routing protocol",
	})
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	reqID := r.Context().Value(reqIDKey{}).(string)
	if r.Method != http.MethodGet {
		s.writeErr(w, http.StatusMethodNotAllowed, reqID, "bad_method", r.Method)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": reqID,
		"snapshot":   s.core.Snapshot(),
	})
}

func (s *Server) handleEvent(w http.ResponseWriter, r *http.Request) {
	reqID := r.Context().Value(reqIDKey{}).(string)
	if r.Method != http.MethodPost {
		s.writeErr(w, http.StatusMethodNotAllowed, reqID, "bad_method", r.Method)
		return
	}
	var in eventRequest
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil {
		s.writeErr(w, http.StatusBadRequest, reqID, "bad_json", err.Error())
		return
	}
	reqID = s.nextRequestID(in.RequestID)

	if in.AtMs < int64(s.clk.Now()) {
		s.writeErr(w, http.StatusUnprocessableEntity, reqID, "clock_monotonic_violation",
			fmt.Sprintf("event at %d is before clock %d; old rounds cannot be replayed",
				in.AtMs, s.clk.Now()))
		return
	}
	if _, _, err := s.core.Tick(model.Millis(in.AtMs)); err != nil {
		s.writeErr(w, http.StatusUnprocessableEntity, reqID, "clock_error", err.Error())
		return
	}

	switch in.Kind {
	case "general_query":
		if !s.cfg.HasInterface(in.Iface) {
			s.writeErr(w, http.StatusUnprocessableEntity, reqID, "unknown_interface",
				"interface "+in.Iface+" is not configured")
			return
		}
		pkt, d, err := s.core.InjectGeneralQuery(in.Iface, reqID)
		if err != nil {
			s.writeErr(w, http.StatusUnprocessableEntity, reqID, "general_query_failed", err.Error())
			return
		}
		if s.st != nil {
			_ = s.st.AppendEmitted(pkt)
			_ = s.st.AppendDiag(d)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"request_id": reqID, "emitted": pkt, "diag": d,
		})
		return
	case "report", "leave":
		// validate through the core
	default:
		s.writeErr(w, http.StatusBadRequest, reqID, "bad_kind",
			"kind must be report, leave or general_query")
		return
	}

	if !s.cfg.HasInterface(in.Iface) {
		s.writeErr(w, http.StatusUnprocessableEntity, reqID, "unknown_interface",
			"interface "+in.Iface+" is not configured")
		return
	}
	kind := model.EvReport
	if in.Kind == "leave" {
		kind = model.EvLeave
	}
	ev := model.Event{
		At:         model.Millis(in.AtMs),
		Kind:       kind,
		Iface:      in.Iface,
		Group:      in.Group,
		Member:     in.Member,
		SourceAddr: in.SourceAddr,
		ResponseTo: in.ResponseTo,
		RequestID:  reqID,
	}
	if s.st != nil {
		_ = s.st.AppendEvent(ev)
	}
	d := s.core.Apply(ev)
	if s.st != nil {
		_ = s.st.AppendDiag(d)
	}
	status := http.StatusOK
	switch d.Verdict {
	case model.VRejected:
		status = http.StatusUnprocessableEntity
	case model.VUndecidable:
		status = http.StatusAccepted // 202: accepted for processing, undecidable
	case model.VStale:
		status = http.StatusConflict
	}
	writeJSON(w, status, diagBody{RequestID: reqID, Diag: d})
}

type tickRequest struct {
	ToMs int64 `json:"to_ms"`
}

func (s *Server) handleTick(w http.ResponseWriter, r *http.Request) {
	reqID := r.Context().Value(reqIDKey{}).(string)
	if r.Method != http.MethodPost {
		s.writeErr(w, http.StatusMethodNotAllowed, reqID, "bad_method", r.Method)
		return
	}
	var in tickRequest
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		s.writeErr(w, http.StatusBadRequest, reqID, "bad_json", err.Error())
		return
	}
	emitted, diags, err := s.core.Tick(model.Millis(in.ToMs))
	if err != nil {
		s.writeErr(w, http.StatusUnprocessableEntity, reqID, "clock_error", err.Error())
		return
	}
	if s.st != nil {
		for _, d := range diags {
			_ = s.st.AppendDiag(d)
		}
		for _, p := range emitted {
			_ = s.st.AppendEmitted(p)
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id":  reqID,
		"now_ms":      int64(s.clk.Now()),
		"emitted":     emitted,
		"timer_diags": diags,
		"snapshot":    s.core.Snapshot(),
	})
}

func (s *Server) handleDiags(w http.ResponseWriter, r *http.Request) {
	reqID := r.Context().Value(reqIDKey{}).(string)
	if s.st == nil {
		writeJSON(w, http.StatusOK, map[string]any{
			"request_id": reqID,
			"diags":      []any{},
			"note":       "storage disabled; use replay mode for the journal",
		})
		return
	}
	diags, err := s.st.Diagnostics()
	if err != nil {
		s.writeErr(w, http.StatusInternalServerError, reqID, "storage_error", err.Error())
		return
	}
	// Return a redacted view: source addresses are never stored in diag
	// payloads here; member names are synthetic fixtures.
	writeJSON(w, http.StatusOK, map[string]any{"request_id": reqID, "diags": diags})
}

func (s *Server) handleIntervals(w http.ResponseWriter, r *http.Request) {
	reqID := r.Context().Value(reqIDKey{}).(string)
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": reqID,
		"intervals":  s.core.Intervals(),
	})
}
