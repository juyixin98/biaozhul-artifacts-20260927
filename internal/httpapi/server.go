// Package httpapi exposes the replay and evidence-query interface over
// HTTP using only the standard library.
//
// Endpoints (all local, JSON unless noted):
//
//	POST /v1/ingest        batch JSON: {"request_id": "...", "packets": [...]}
//	POST /v1/ingest/jsonl  raw JSONL capture (one packet per line)
//	GET  /v1/connections    list flows and handshake generations
//	GET  /v1/stream         reassembled bytes: ?flow_key&gen&direction&start&end&format=hex|raw
//	GET  /v1/gaps           gap evidence: ?flow_key&gen&direction&status
//	GET  /v1/conflicts      conflict ledger: ?flow_key&gen&direction
//	GET  /v1/diagnostics    decision records: ?request_id&flow_key&gen&category&limit
//	GET  /healthz
package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"

	"tcpreasm/internal/config"
	"tcpreasm/internal/reassembly"
	"tcpreasm/internal/store"
	"tcpreasm/internal/tcpmodel"
)

// Server bundles dependencies for the handlers.
type Server struct {
	Engine *reassembly.Engine
	Store  *store.Store
	Cfg    config.Config
	Logger *log.Logger
}

// NewRouter builds the mux.
func (s *Server) NewRouter() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/ingest", s.handleIngest)
	mux.HandleFunc("POST /v1/ingest/jsonl", s.handleIngestJSONL)
	mux.HandleFunc("GET /v1/connections", s.handleConnections)
	mux.HandleFunc("GET /v1/stream", s.handleStream)
	mux.HandleFunc("GET /v1/gaps", s.handleGaps)
	mux.HandleFunc("GET /v1/conflicts", s.handleConflicts)
	mux.HandleFunc("GET /v1/diagnostics", s.handleDiagnostics)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return s.requestID(s.maxBytes(mux))
}

// ---- ingest -----------------------------------------------------------------------

type ingestRequest struct {
	RequestID string            `json:"request_id"`
	Packets   []tcpmodel.Packet `json:"packets"`
}

type packetVerdict struct {
	RecordID  string `json:"record_id,omitempty"`
	Order     int64  `json:"order"`
	Decision  string `json:"decision"`
	Category  string `json:"category"`
	Reason    string `json:"reason"`
	FlowKey   string `json:"flow_key,omitempty"`
	GenIndex  int    `json:"gen_index,omitempty"`
	Direction string `json:"direction,omitempty"`
	Inferred  bool   `json:"inferred,omitempty"`
	// Delivered reports bytes newly emitted while processing this packet.
	Delivered       []deliveredDTO `json:"delivered,omitempty"`
	DupBytes        int            `json:"dup_bytes,omitempty"`
	RejectedBytes   int            `json:"rejected_bytes,omitempty"`
	DuplicatePacket bool           `json:"duplicate_packet,omitempty"`
}

type deliveredDTO struct {
	Direction string `json:"direction"`
	StreamOff uint64 `json:"stream_off"`
	Length    int    `json:"length"`
	RecordID  string `json:"record_id"`
}

type ingestResponse struct {
	RequestID   string           `json:"request_id"`
	Accepted    int              `json:"accepted"`
	Rejected    int              `json:"rejected"`
	Undecidable int              `json:"undecidable"`
	Verdicts    []packetVerdict  `json:"verdicts"`
	Stats       reassembly.Stats `json:"stats"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, s.Cfg.HTTP.MaxCaptureBytes)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	s.runIngest(w, r, req.RequestID, req.Packets)
}

func (s *Server) handleIngestJSONL(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, s.Cfg.HTTP.MaxCaptureBytes))
	if err != nil {
		writeError(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	pkts, err := tcpmodel.ParseCapture(body)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	s.runIngest(w, r, r.Header.Get("X-Request-Id"), pkts)
}

func (s *Server) runIngest(w http.ResponseWriter, r *http.Request, requestID string, pkts []tcpmodel.Packet) {
	if requestID == "" {
		requestID = requestIDFromCtx(r)
	}
	resp := ingestResponse{RequestID: requestID, Verdicts: make([]packetVerdict, 0, len(pkts))}
	// Capture order: explicit Order wins, otherwise preserve body order.
	for i := range pkts {
		if pkts[i].Order == 0 {
			pkts[i].Order = int64(i) + 1
		}
		if pkts[i].RecordID == "" {
			pkts[i].RecordID = fmt.Sprintf("auto-%d", pkts[i].Order)
		}
	}
	sortPackets(pkts)
	for _, p := range pkts {
		res, err := s.Engine.Process(r.Context(), p, requestID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "process packet "+p.RecordID+": "+err.Error())
			return
		}
		v := packetVerdict{
			RecordID: p.RecordID, Order: p.Order,
			Decision: string(res.Decision), Category: string(res.Category), Reason: res.Reason,
			FlowKey: res.FlowKey, GenIndex: res.GenIndex, Direction: res.Direction,
			Inferred: res.Inferred, DuplicatePacket: res.DuplicatePacket,
		}
		for _, dr := range res.DirResults {
			v.DupBytes += dr.BytesDedup
			v.RejectedBytes += dr.BytesRejected
			for _, c := range dr.Chunks {
				v.Delivered = append(v.Delivered, deliveredDTO{
					Direction: c.Direction, StreamOff: c.StreamOff,
					Length: len(c.Data), RecordID: c.RecordID,
				})
			}
		}
		switch res.Decision {
		case "ACCEPTED":
			resp.Accepted++
		case "REJECTED":
			resp.Rejected++
		case "UNDECIDABLE":
			resp.Undecidable++
		}
		resp.Verdicts = append(resp.Verdicts, v)
	}
	resp.Stats = s.Engine.Stats()
	writeJSON(w, http.StatusOK, resp)
}

// sortPackets orders by (Order, RecordID) without pulling in sort at every
// caller: stable insertion sort, captures are small; large captures are
// still O(n^2) worst case so use a simple stdlib sort instead.
func sortPackets(p []tcpmodel.Packet) {
	// Kept in a helper to centralize ordering rules.
	for i := 1; i < len(p); i++ {
		for j := i; j > 0 && lessPacket(p[j], p[j-1]); j-- {
			p[j], p[j-1] = p[j-1], p[j]
		}
	}
}

func lessPacket(a, b tcpmodel.Packet) bool {
	if a.Order != b.Order {
		return a.Order < b.Order
	}
	return a.RecordID < b.RecordID
}

// ---- queries ----------------------------------------------------------------------

func (s *Server) handleConnections(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeError(w, http.StatusServiceUnavailable, "storage not configured")
		return
	}
	conns, err := s.Store.ListConnections(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if conns == nil {
		conns = []store.ConnectionOut{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"connections": conns})
}

type streamResponse struct {
	FlowKey         string `json:"flow_key"`
	GenIndex        int    `json:"gen_index"`
	Direction       string `json:"direction"`
	StartOff        uint64 `json:"start_off"`
	EndOffRequested uint64 `json:"end_off_requested"`
	Contiguous      bool   `json:"contiguous"`
	TotalDelivered  uint64 `json:"total_delivered"`
	Length          int    `json:"length"`
	DataHex         string `json:"data_hex"`
}

func (s *Server) handleStream(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeError(w, http.StatusServiceUnavailable, "storage not configured")
		return
	}
	q := r.URL.Query()
	flowKey := q.Get("flow_key")
	gen, err := strconv.Atoi(q.Get("gen"))
	if err != nil || gen <= 0 {
		writeError(w, http.StatusBadRequest, "gen must be a positive integer")
		return
	}
	dir := q.Get("direction")
	if dir != "c2s" && dir != "s2c" {
		writeError(w, http.StatusBadRequest, "direction must be c2s or s2c")
		return
	}
	start, _ := strconv.ParseUint(q.Get("start"), 10, 64)
	end, err := strconv.ParseUint(q.Get("end"), 10, 64)
	if err != nil || end <= start {
		writeError(w, http.StatusBadRequest, "end must be a positive offset greater than start")
		return
	}
	limit := -1
	if l := q.Get("limit"); l != "" {
		limit, err = strconv.Atoi(l)
		if err != nil || limit <= 0 {
			writeError(w, http.StatusBadRequest, "limit must be a positive integer")
			return
		}
	}
	data, contig, total, err := s.Store.StreamByteRange(r.Context(), flowKey, gen, dir, start, end, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if q.Get("format") == "raw" {
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("X-Contiguous", strconv.FormatBool(contig))
		w.Header().Set("X-Total-Delivered", strconv.FormatUint(total, 10))
		_, _ = w.Write(data)
		return
	}
	writeJSON(w, http.StatusOK, streamResponse{
		FlowKey: flowKey, GenIndex: gen, Direction: dir,
		StartOff: start, EndOffRequested: end,
		Contiguous: contig, TotalDelivered: total,
		Length: len(data), DataHex: hex.EncodeToString(data),
	})
}

func (s *Server) handleGaps(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeError(w, http.StatusServiceUnavailable, "storage not configured")
		return
	}
	q := r.URL.Query()
	flowKey := q.Get("flow_key")
	gen, err := strconv.Atoi(q.Get("gen"))
	if err != nil || gen <= 0 {
		writeError(w, http.StatusBadRequest, "gen must be a positive integer")
		return
	}
	if flowKey == "" {
		writeError(w, http.StatusBadRequest, "flow_key is required")
		return
	}
	gaps, err := s.Store.ListGaps(r.Context(), flowKey, gen, q.Get("direction"), q.Get("status"))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if gaps == nil {
		gaps = []store.GapRow{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"gaps": gaps})
}

func (s *Server) handleConflicts(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeError(w, http.StatusServiceUnavailable, "storage not configured")
		return
	}
	q := r.URL.Query()
	flowKey := q.Get("flow_key")
	if flowKey == "" {
		writeError(w, http.StatusBadRequest, "flow_key is required")
		return
	}
	gen, err := strconv.Atoi(q.Get("gen"))
	if err != nil || gen <= 0 {
		writeError(w, http.StatusBadRequest, "gen must be a positive integer")
		return
	}
	conflicts, err := s.Store.ListConflicts(r.Context(), flowKey, gen, q.Get("direction"))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if conflicts == nil {
		conflicts = []store.ConflictOut{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"conflicts": conflicts})
}

func (s *Server) handleDiagnostics(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeError(w, http.StatusServiceUnavailable, "storage not configured")
		return
	}
	q := r.URL.Query()
	gen := -1
	if g := q.Get("gen"); g != "" {
		v, err := strconv.Atoi(g)
		if err != nil || v < 0 {
			writeError(w, http.StatusBadRequest, "gen must be >= 0")
			return
		}
		gen = v
	}
	limit := 500
	if l := q.Get("limit"); l != "" {
		v, err := strconv.Atoi(l)
		if err != nil || v <= 0 || v > 10000 {
			writeError(w, http.StatusBadRequest, "limit must be in [1,10000]")
			return
		}
		limit = v
	}
	recs, err := s.Store.ListDiagnostics(r.Context(),
		q.Get("request_id"), q.Get("flow_key"), gen, q.Get("category"), limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if recs == nil {
		recs = []store.DiagOut{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"diagnostics": recs})
}

// ---- plumbing ---------------------------------------------------------------------

type ctxKey string

const reqIDKey ctxKey = "request_id"

func (s *Server) requestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-Id")
		if id == "" {
			id = "req-" + strconv.FormatInt(randish(), 36)
		}
		r = r.WithContext(context.WithValue(r.Context(), reqIDKey, id))
		w.Header().Set("X-Request-Id", id)
		next.ServeHTTP(w, r)
	})
}

func (s *Server) maxBytes(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			r.Body = http.MaxBytesReader(w, r.Body, s.Cfg.HTTP.MaxCaptureBytes)
		}
		next.ServeHTTP(w, r)
	})
}

func requestIDFromCtx(r *http.Request) string {
	if v, ok := r.Context().Value(reqIDKey).(string); ok {
		return v
	}
	return ""
}

type apiError struct {
	Error string `json:"error"`
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, apiError{Error: msg})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

// randish returns a best-effort non-crypto id component.
func randish() int64 {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return 0
	}
	return int64(binary.LittleEndian.Uint64(b[:]))
}
