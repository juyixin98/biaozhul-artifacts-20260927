// Package server exposes the NAT engine over a local HTTP replay API using
// only the standard library. Nothing here touches the host network stack: it
// is an in-process model reached through 127.0.0.1.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"natlab/internal/model"
	"natlab/internal/nat"
)

// Server holds the HTTP handlers and their engine.
type Server struct {
	eng *nat.Engine
	mux *http.ServeMux
}

// New builds the HTTP handler tree.
func New(eng *nat.Engine) *Server {
	s := &Server{eng: eng, mux: http.NewServeMux()}
	s.mux.HandleFunc("GET /healthz", s.health)
	s.mux.HandleFunc("POST /runs/{runID}/packets", s.evaluate)
	s.mux.HandleFunc("POST /runs/{runID}/packets/batch", s.batch)
	s.mux.HandleFunc("GET /runs/{runID}/events", s.events)
	s.mux.HandleFunc("GET /runs/{runID}/mappings", s.mappings)
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// packetRequest is the wire input. Five-tuple fields are flat.
type packetRequest struct {
	Seq       int64           `json:"seq,omitempty"`
	TS        time.Time       `json:"ts"`
	SrcIP     string          `json:"src_ip"`
	SrcPort   uint16          `json:"src_port"`
	DstIP     string          `json:"dst_ip"`
	DstPort   uint16          `json:"dst_port"`
	Protocol  model.Protocol  `json:"protocol"`
	Direction model.Direction `json:"direction"`
	Flags     string          `json:"flags,omitempty"`
	Fragment  *model.FragInfo `json:"fragment,omitempty"`
}

func (r packetRequest) toPacket() model.Packet {
	return model.Packet{
		Seq: r.Seq, ObservedAt: r.TS, Direction: r.Direction, Flags: r.Flags, Fragment: r.Fragment,
		FiveTuple: model.FiveTuple{
			SrcIP: r.SrcIP, SrcPort: r.SrcPort, DstIP: r.DstIP,
			DstPort: r.DstPort, Protocol: r.Protocol,
		},
	}
}

// response is the stable verdict envelope. Category/code are empty on accept.
type response struct {
	RunID       string           `json:"run_id"`
	Seq         int64            `json:"seq,omitempty"`
	Accepted    bool             `json:"accepted"`
	Category    model.Category   `json:"category,omitempty"`
	Code        string           `json:"code,omitempty"`
	Reason      string           `json:"reason,omitempty"`
	ObservedAt  time.Time        `json:"observed_at"`
	EffectiveAt *time.Time       `json:"effective_at,omitempty"`
	ClockRewind bool             `json:"clock_rewind,omitempty"`
	Swept       int64            `json:"swept_expired,omitempty"`
	ActiveCount int              `json:"active_mappings,omitempty"`
	MappedPort  uint16           `json:"mapped_port,omitempty"`
	State       string           `json:"state,omitempty"`
	Translated  *model.FiveTuple `json:"translated,omitempty"`
}

func (s *Server) evaluate(w http.ResponseWriter, req *http.Request) {
	runID := req.PathValue("runID")
	if runID == "" {
		writeError(w, http.StatusBadRequest, model.CatInvalidInput, model.CodeUnknownRun, "empty run id")
		return
	}
	var in packetRequest
	if err := json.NewDecoder(req.Body).Decode(&in); err != nil {
		writeError(w, http.StatusBadRequest, model.CatInvalidInput, "BAD_JSON", "malformed JSON body: "+err.Error())
		return
	}
	pkt := in.toPacket()
	res, err := s.eng.Evaluate(req.Context(), runID, pkt)
	if err != nil {
		var ce *nat.ComputeError
		if errors.As(err, &ce) {
			writeError(w, http.StatusInternalServerError, model.CatComputeFailure, model.CodeStoreError, ce.Error())
			return
		}
		writeError(w, http.StatusInternalServerError, model.CatComputeFailure, model.CodeStoreError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, buildResponse(runID, pkt, res))
}

func (s *Server) batch(w http.ResponseWriter, req *http.Request) {
	runID := req.PathValue("runID")
	if runID == "" {
		writeError(w, http.StatusBadRequest, model.CatInvalidInput, model.CodeUnknownRun, "empty run id")
		return
	}
	var in []packetRequest
	if err := json.NewDecoder(req.Body).Decode(&in); err != nil {
		writeError(w, http.StatusBadRequest, model.CatInvalidInput, "BAD_JSON", "malformed JSON array: "+err.Error())
		return
	}
	out := make([]response, 0, len(in))
	for _, item := range in {
		pkt := item.toPacket()
		res, err := s.eng.Evaluate(req.Context(), runID, pkt)
		if err != nil {
			writeError(w, http.StatusInternalServerError, model.CatComputeFailure, model.CodeStoreError, err.Error())
			return
		}
		out = append(out, buildResponse(runID, pkt, res))
	}
	writeJSON(w, http.StatusOK, out)
}

func buildResponse(runID string, pkt model.Packet, res *nat.RunResult) response {
	d := res.Decision
	r := response{
		RunID: runID, Seq: pkt.Seq, Accepted: d.Accepted, Category: d.Category,
		Code: d.Code, Reason: d.Reason, ObservedAt: d.ObservedAt,
		ClockRewind: d.ClockRewind, Swept: d.Swept, ActiveCount: d.ActiveCount,
		Translated: d.Translated,
	}
	if !d.EffectiveAt.IsZero() {
		eff := d.EffectiveAt
		r.EffectiveAt = &eff
	}
	if d.Mapping != nil {
		r.MappedPort = d.Mapping.MappedPort
		r.State = d.Mapping.State
	}
	return r
}

type eventView struct {
	ID          int64           `json:"id"`
	Seq         int64           `json:"seq"`
	ObservedAt  time.Time       `json:"observed_at"`
	EffectiveAt time.Time       `json:"effective_at"`
	ClockRewind bool            `json:"clock_rewind"`
	Accepted    bool            `json:"accepted"`
	Category    model.Category  `json:"category,omitempty"`
	Code        string          `json:"code,omitempty"`
	Reason      string          `json:"reason"`
	MappedPort  uint16          `json:"mapped_port,omitempty"`
	State       string          `json:"state,omitempty"`
	Detail      json.RawMessage `json:"detail,omitempty"`
	Packet      model.Packet    `json:"packet"`
}

func (s *Server) events(w http.ResponseWriter, req *http.Request) {
	runID := req.PathValue("runID")
	evs, err := s.eng.ListEvents(req.Context(), runID, queryLimit(req))
	if err != nil {
		writeError(w, http.StatusInternalServerError, model.CatComputeFailure, model.CodeStoreError, err.Error())
		return
	}
	out := make([]eventView, 0, len(evs))
	for _, e := range evs {
		v := eventView{
			ID: e.ID, Seq: e.Seq, ObservedAt: e.ObservedAt, EffectiveAt: e.EffectiveAt,
			ClockRewind: e.ClockRewind, Accepted: e.Accepted, Category: e.Category,
			Code: e.Code, Reason: e.Reason, MappedPort: e.MappedPort, State: e.State,
			Packet: e.Packet,
		}
		if e.Detail != "" {
			v.Detail = json.RawMessage(e.Detail)
		}
		out = append(out, v)
	}
	writeJSON(w, http.StatusOK, out)
}

type mappingView struct {
	ID         int64          `json:"id"`
	Protocol   model.Protocol `json:"protocol"`
	SrcIP      string         `json:"src_ip"`
	SrcPort    uint16         `json:"src_port"`
	DstIP      string         `json:"dst_ip"`
	DstPort    uint16         `json:"dst_port"`
	MappedPort uint16         `json:"mapped_port"`
	State      string         `json:"state"`
	CreatedAt  time.Time      `json:"created_at"`
	LastUsedAt time.Time      `json:"last_used_at"`
	ExpiresAt  time.Time      `json:"expires_at"`
}

func (s *Server) mappings(w http.ResponseWriter, req *http.Request) {
	runID := req.PathValue("runID")
	activeOnly := req.URL.Query().Get("active") == "true"
	ms, err := s.eng.ListMappings(req.Context(), runID, activeOnly)
	if err != nil {
		writeError(w, http.StatusInternalServerError, model.CatComputeFailure, model.CodeStoreError, err.Error())
		return
	}
	out := make([]mappingView, 0, len(ms))
	for _, m := range ms {
		out = append(out, mappingView{
			ID: m.ID, Protocol: m.Protocol, SrcIP: m.SrcIP, SrcPort: m.SrcPort,
			DstIP: m.DstIP, DstPort: m.DstPort, MappedPort: m.MappedPort, State: m.State,
			CreatedAt: m.CreatedAt, LastUsedAt: m.LastUsedAt, ExpiresAt: m.ExpiresAt,
			// No wall-clock "expired" bool: replay runs use synthetic timestamps;
			// a caller compares expires_at to the run's effective clock.
		})
	}
	writeJSON(w, http.StatusOK, out)
}

func queryLimit(req *http.Request) int {
	l := req.URL.Query().Get("limit")
	if l == "" {
		return 0
	}
	var n int
	if _, err := fmt.Sscanf(l, "%d", &n); err != nil || n <= 0 {
		return 0
	}
	return n
}

type errBody struct {
	Category model.Category `json:"category"`
	Code     string         `json:"code"`
	Reason   string         `json:"reason"`
}

func writeError(w http.ResponseWriter, status int, cat model.Category, code, reason string) {
	writeJSON(w, status, errBody{Category: cat, Code: code, Reason: reason})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
