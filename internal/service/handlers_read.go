package service

import (
	"errors"
	"net/http"
	"net/url"
	"strconv"

	"tcpreplay/internal/storage"
)

func (s *Server) handleListRequests(w http.ResponseWriter, r *http.Request) {
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 || n > 1000 {
			writeError(w, http.StatusBadRequest, "validation", "limit must be in 1..1000")
			return
		}
		limit = n
	}
	headers, err := s.store.ListRequests(limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"requests": headers})
}

func (s *Server) handleGetRequest(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	h, err := s.store.GetRequest(id)
	if errors.Is(err, storage.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "unknown request id")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, h)
}

func (s *Server) handleReport(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	h, err := s.store.GetRequest(id)
	if errors.Is(err, storage.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "unknown request id")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	views, err := s.store.ListViews(id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	events, err := s.store.ListEvents(id, storage.EventFilter{})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	conflicts, err := s.store.ListConflicts(id, "", "", -1)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	packets, err := s.store.ListPackets(id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, Report{
		Request: h, Views: views, Events: events,
		Conflicts: conflicts, Packets: packets,
	})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, err := s.store.GetRequest(id); errors.Is(err, storage.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "unknown request id")
		return
	} else if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	f := storage.EventFilter{
		Code:      r.URL.Query().Get("code"),
		Level:     r.URL.Query().Get("level"),
		Flow:      r.URL.Query().Get("flow"),
		Direction: r.URL.Query().Get("direction"),
	}
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			writeError(w, http.StatusBadRequest, "validation", "limit must be a positive integer")
			return
		}
		f.Limit = n
	}
	events, err := s.store.ListEvents(id, f)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"request_id": id, "events": events})
}

func (s *Server) handleConflicts(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, err := s.store.GetRequest(id); errors.Is(err, storage.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "unknown request id")
		return
	} else if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	gen := -1
	if v := r.URL.Query().Get("generation"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "validation", "generation must be a non-negative integer")
			return
		}
		gen = n
	}
	conflicts, err := s.store.ListConflicts(id,
		r.URL.Query().Get("flow"), r.URL.Query().Get("direction"), gen)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"request_id": id, "conflicts": conflicts})
}

func (s *Server) handlePackets(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, err := s.store.GetRequest(id); errors.Is(err, storage.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "unknown request id")
		return
	} else if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	packets, err := s.store.ListPackets(id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"request_id": id, "packets": packets})
}

// handleStream serves the replayed, contiguous bytes of one generation
// direction. The default response is application/octet-stream containing only
// proved bytes; gap/held evidence is summarized in X-Tcpreplay-* headers.
// ?format=json returns the full DirectionView instead (base64 stream plus
// gaps, held runs, quarantine).
func (s *Server) handleStream(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	flowRaw := r.PathValue("flow")
	flow, err := url.PathUnescape(flowRaw)
	if err != nil {
		writeError(w, http.StatusBadRequest, "validation", "bad flow encoding")
		return
	}
	gen, err := strconv.Atoi(r.PathValue("gen"))
	if err != nil || gen < 0 {
		writeError(w, http.StatusBadRequest, "validation", "generation must be a non-negative integer")
		return
	}
	direction := r.PathValue("direction")
	if direction != "a_to_b" && direction != "b_to_a" {
		writeError(w, http.StatusBadRequest, "validation", "direction must be a_to_b or b_to_a")
		return
	}
	view, err := s.store.GetView(id, flow, gen)
	if errors.Is(err, storage.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found",
			"no such request/flow/generation (note: flow must be URL-encoded, e.g. use the report's flow string)")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	dv := view.AtoB
	if direction == "b_to_a" {
		dv = view.BtoA
	}
	if r.URL.Query().Get("format") == "json" {
		writeJSON(w, http.StatusOK, dv)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Tcpreplay-Request-Id", id)
	w.Header().Set("X-Tcpreplay-Generation", strconv.Itoa(gen))
	w.Header().Set("X-Tcpreplay-Handshake-Known", strconv.FormatBool(dv.HandshakeKnown))
	w.Header().Set("X-Tcpreplay-Fin-Seen", strconv.FormatBool(dv.FINSeen))
	w.Header().Set("X-Tcpreplay-Gap-Count", strconv.Itoa(len(dv.Gaps)))
	w.Header().Set("X-Tcpreplay-Held-Count", strconv.Itoa(len(dv.HeldRuns)))
	w.Header().Set("X-Tcpreplay-Content-Only", "contiguous-evidenced-prefix")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(dv.Stream)
}
