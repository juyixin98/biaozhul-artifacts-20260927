package service

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"tcpreplay/internal/config"
	"tcpreplay/internal/diagnose"
	"tcpreplay/internal/netmodel"
	"tcpreplay/internal/storage"
)

// Server holds dependencies for the HTTP API.
type Server struct {
	store *storage.Store
	cfg   config.Config
	diag  *diagnose.Logger
	mux   *http.ServeMux
}

// NewServer constructs the API router.
func NewServer(store *storage.Store, cfg config.Config, diag *diagnose.Logger) *Server {
	s := &Server{store: store, cfg: cfg, diag: diag, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler exposes the configured router.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	s.mux.HandleFunc("POST /api/v1/ingest", s.handleIngest)
	s.mux.HandleFunc("POST /api/v1/ingest/pcap", s.handleIngestPCap)
	s.mux.HandleFunc("GET /api/v1/requests", s.handleListRequests)
	s.mux.HandleFunc("GET /api/v1/requests/{id}", s.handleGetRequest)
	s.mux.HandleFunc("GET /api/v1/requests/{id}/report", s.handleReport)
	s.mux.HandleFunc("GET /api/v1/requests/{id}/events", s.handleEvents)
	s.mux.HandleFunc("GET /api/v1/requests/{id}/conflicts", s.handleConflicts)
	s.mux.HandleFunc("GET /api/v1/requests/{id}/packets", s.handlePackets)
	s.mux.HandleFunc("GET /api/v1/requests/{id}/flows/{flow}/generations/{gen}/stream/{direction}", s.handleStream)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type apiError struct {
	Error     string `json:"error"`
	RequestID string `json:"request_id,omitempty"`
	Category  string `json:"category"`
}

func writeError(w http.ResponseWriter, status int, category, msg string) {
	writeJSON(w, status, apiError{Error: msg, Category: category})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func newRequestID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return "req-" + hex.EncodeToString(b[:]), nil
}

// ingest runs the full analysis pipeline for an ordered packet list.
func (s *Server) ingest(requestID, source string, packets []netmodel.Packet) (IngestResponse, int, *apiError) {
	if requestID == "" {
		var err error
		requestID, err = newRequestID()
		if err != nil {
			return IngestResponse{}, http.StatusInternalServerError,
				&apiError{Category: "internal", Error: "generate request id: " + err.Error()}
		}
	}
	if len(requestID) > 128 {
		return IngestResponse{}, http.StatusBadRequest,
			&apiError{RequestID: requestID, Category: "validation", Error: "request_id too long (max 128)"}
	}
	if len(packets) == 0 {
		return IngestResponse{}, http.StatusBadRequest,
			&apiError{RequestID: requestID, Category: "validation", Error: "no packets provided"}
	}
	if len(packets) > s.cfg.MaxPacketsPerRequest {
		return IngestResponse{}, http.StatusRequestEntityTooLarge,
			&apiError{RequestID: requestID, Category: "validation",
				Error: fmt.Sprintf("packet count %d exceeds max %d", len(packets), s.cfg.MaxPacketsPerRequest)}
	}
	if exists, err := s.store.Exists(requestID); err != nil {
		return IngestResponse{}, http.StatusInternalServerError,
			&apiError{RequestID: requestID, Category: "internal", Error: err.Error()}
	} else if exists {
		return IngestResponse{}, http.StatusConflict,
			&apiError{RequestID: requestID, Category: "duplicate_request", Error: "request_id already analyzed"}
	}
	for i, p := range packets {
		if err := validatePacket(p); err != nil {
			return IngestResponse{}, http.StatusBadRequest, &apiError{
				RequestID: requestID, Category: "validation",
				Error: fmt.Sprintf("packet[%d] record_id=%q: %v", i, p.RecordID, err),
			}
		}
	}

	run := newAnalysisRun(s.cfg.OverlapPolicy, s.cfg.PayloadPreview)
	logger := s.diag.RequestBound(requestID)
	for i, p := range packets {
		run.feed(i, p, requestID)
	}
	for _, e := range run.events {
		logger.Event(e)
	}

	views := run.manager.AllViews()
	conflicts := run.conflicts()
	rec := storage.RequestRecord{
		ID: requestID, Source: source, Policy: s.cfg.OverlapPolicy,
		Preview: s.cfg.PayloadPreview, PacketCount: len(packets),
	}
	if err := s.store.SaveAnalysis(rec, run.metas, run.events, conflicts, views); err != nil {
		return IngestResponse{}, http.StatusInternalServerError,
			&apiError{RequestID: requestID, Category: "internal", Error: "persist analysis: " + err.Error()}
	}

	resp := IngestResponse{
		RequestID: requestID, Source: source, Policy: s.cfg.OverlapPolicy,
		PacketCount: len(packets), CreatedAt: run.createdAt(),
		EventsByLevel: map[string]int{},
	}
	resp.Conflicts = len(conflicts)
	for _, e := range run.events {
		resp.EventsByLevel[string(e.Level)]++
	}
	for _, v := range views {
		resp.Generations = append(resp.Generations, GenerationSummary{
			Flow: v.Flow, Generation: v.Generation, Closed: v.Closed, Reset: v.Reset,
			AtoBBytes: int64(len(v.AtoB.Stream)), BtoABytes: int64(len(v.BtoA.Stream)),
			AtoBGaps: len(v.AtoB.Gaps), BtoAGaps: len(v.BtoA.Gaps),
		})
	}
	return resp, 0, nil
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	var req IngestRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, s.cfg.MaxUploadBytes))
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "validation", "decode JSON: "+err.Error())
		return
	}
	if dec.More() {
		writeError(w, http.StatusBadRequest, "validation", "unexpected trailing JSON content")
		return
	}
	resp, status, perr := s.ingest(req.RequestID, "json", req.Packets)
	if perr != nil {
		writeError(w, status, perr.Category, perr.Error)
		return
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (s *Server) handleIngestPCap(w http.ResponseWriter, r *http.Request) {
	requestID := r.URL.Query().Get("request_id")
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, s.cfg.MaxUploadBytes))
	if err != nil {
		writeError(w, http.StatusBadRequest, "validation", "read body: "+err.Error())
		return
	}
	if len(body) == 0 {
		writeError(w, http.StatusBadRequest, "validation", "empty pcap body")
		return
	}
	source := r.URL.Query().Get("source")
	if source == "" {
		source = "pcap-upload"
	}
	packets, err := netmodel.ParsePCap(body)
	if err != nil {
		writeError(w, http.StatusBadRequest, "pcap_parse_failed", err.Error())
		return
	}
	if len(packets) == 0 {
		writeError(w, http.StatusBadRequest, "pcap_no_tcp", "capture contains no parseable TCP segments")
		return
	}
	resp, status, perr := s.ingest(requestID, source, packets)
	if perr != nil {
		writeError(w, status, perr.Category, perr.Error)
		return
	}
	writeJSON(w, http.StatusCreated, resp)
}

func validatePacket(p netmodel.Packet) error {
	if p.SrcIP == "" || p.DstIP == "" {
		return errors.New("src_ip and dst_ip are required")
	}
	if p.SrcPort == 0 || p.DstPort == 0 {
		return errors.New("src_port and dst_port must be non-zero")
	}
	return nil
}
