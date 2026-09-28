// Package service wires the netmodel engine to the HTTP API and persists every
// outcome (success or typed failure) with a correlation request id.
package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"runtime"
	"strings"
	"time"

	"cidrsvc/internal/config"
	"cidrsvc/internal/netmodel"
	"cidrsvc/internal/store"
)

// Version identifies the running build; overridden at link time in CI.
var Version = "dev"

// Service holds dependencies for the HTTP handlers.
type Service struct {
	cfg   config.Config
	store store.Store
	log   Logger
	now   func() time.Time
	idGen func() string
}

// New constructs a Service.
func New(cfg config.Config, st store.Store, log Logger) *Service {
	return &Service{
		cfg:   cfg,
		store: st,
		log:   log,
		now:   func() time.Time { return time.Now().UTC() },
		idGen: newRequestID,
	}
}

// Routes returns the wired mux.
func (s *Service) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/compute", s.withIdentity(s.handleCompute))
	mux.HandleFunc("GET /v1/requests/{id}", s.withIdentity(s.handleGet))
	mux.HandleFunc("GET /v1/requests", s.withIdentity(s.handleList))
	mux.HandleFunc("GET /v1/stats", s.withIdentity(s.handleStats))
	mux.HandleFunc("GET /healthz", s.handleHealth)
	return s.recoverer(mux)
}

// requestContext carries correlation identity through a request.
type requestContext struct {
	ID        string
	ClientRef string
}

type ctxKey int

const rcKey ctxKey = iota

// withIdentity assigns/carries the correlation id and emits one access log.
func (s *Service) withIdentity(h func(http.ResponseWriter, *http.Request)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimSpace(r.Header.Get("X-Request-Id"))
		if id == "" {
			id = s.idGen()
		}
		rc := requestContext{ID: id, ClientRef: strings.TrimSpace(r.Header.Get("X-Client-Ref"))}
		ctx := context.WithValue(r.Context(), rcKey, rc)
		w.Header().Set("X-Request-Id", id)
		start := s.now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		h(sw, r.WithContext(ctx))
		s.log.Log(LogEntry{
			Time:       s.now(),
			Level:      "info",
			RequestID:  id,
			ClientRef:  rc.ClientRef,
			Message:    "http_access",
			Method:     r.Method,
			Path:       r.URL.Path,
			Status:     sw.status,
			DurationMS: s.now().Sub(start).Milliseconds(),
			Version:    Version,
		})
	}
}

func identity(r *http.Request) requestContext {
	if rc, ok := r.Context().Value(rcKey).(requestContext); ok {
		return rc
	}
	return requestContext{ID: "unknown"}
}

func newRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// rand.Read failing is catastrophic; fall back to time but keep shape.
		return fmt.Sprintf("req-%d", time.Now().UnixNano())
	}
	return "req_" + hex.EncodeToString(b[:])
}

// ---- DTOs ----------------------------------------------------------------

type computeRequestDTO struct {
	Family  string   `json:"family"`
	Allow   []string `json:"allow"`
	Exclude []string `json:"exclude"`
	Strict  *bool    `json:"strict"`
}

type proofDTO struct {
	Width              int      `json:"width"`
	TargetAddressCount string   `json:"target_address_count"`
	CoverAddressCount  string   `json:"cover_address_count"`
	BlockCount         int      `json:"block_count"`
	ExactlyEquivalent  bool     `json:"exactly_equivalent"`
	Overlaps           []string `json:"overlaps,omitempty"`
	MergeableSiblings  []string `json:"mergeable_siblings,omitempty"`
}

type stepDTO struct {
	Name       string `json:"name"`
	Detail     string `json:"detail"`
	DurationUS int64  `json:"duration_us"`
	Location   string `json:"location"`
}

type computeResponseDTO struct {
	RequestID string    `json:"request_id"`
	Family    string    `json:"family"`
	Width     int       `json:"width"`
	Status    string    `json:"status"`
	Prefixes  []string  `json:"prefixes"`
	Empty     bool      `json:"empty"`
	Proof     proofDTO  `json:"proof"`
	Warnings  []string  `json:"warnings"`
	Steps     []stepDTO `json:"steps"`
	Version   string    `json:"version"`
}

type errorDTO struct {
	RequestID string `json:"request_id"`
	Status    string `json:"status"`
	ErrorCode string `json:"error_code"`
	Error     string `json:"error"`
	Location  string `json:"location"`
	Version   string `json:"version"`
}

// ---- handlers ------------------------------------------------------------

func (s *Service) handleCompute(w http.ResponseWriter, r *http.Request) {
	rc := identity(r)
	var dto computeRequestDTO
	if err := decodeJSONLimited(w, r, &dto, 4<<20); err != nil {
		s.fail(w, r, http.StatusBadRequest, "invalid_json", err)
		return
	}
	if dto.Allow == nil {
		dto.Allow = []string{}
	}
	if dto.Exclude == nil {
		dto.Exclude = []string{}
	}
	if len(dto.Allow)+len(dto.Exclude) > s.cfg.MaxInputPrefixes {
		s.fail(w, r, http.StatusBadRequest, "too_many_prefixes", fmt.Errorf(
			"allow+exclude %d exceeds configured max %d",
			len(dto.Allow)+len(dto.Exclude), s.cfg.MaxInputPrefixes))
		return
	}
	strict := s.cfg.StrictCIDR
	if dto.Strict != nil {
		strict = *dto.Strict
	}

	out, err := netmodel.Compute(netmodel.ComputeRequest{
		Family: dto.Family, Allow: dto.Allow, Exclude: dto.Exclude, Strict: strict,
	})

	created := s.now()
	if err != nil {
		code := "compute_failed"
		var pe *netmodel.PrefixError
		if errors.As(err, &pe) {
			code = pe.Kind
		}
		var vf *netmodel.VerificationFailure
		if errors.As(err, &vf) {
			code = "verification_failure"
		}
		s.persist(r.Context(), rc, dto, store.Record{
			RequestID: rc.ID, Status: "error", ErrorCode: code, ErrorText: err.Error(),
			ClientRef: rc.ClientRef, CreatedAt: created,
		})
		s.log.Log(LogEntry{
			Time: s.now(), Level: "error", RequestID: rc.ID, ClientRef: rc.ClientRef,
			Message: "compute_failed", ErrorCode: code, Error: err.Error(),
			Location: callerLocation(2), Version: Version,
		})
		s.fail(w, r, httpStatusFor(code), code, err)
		return
	}

	resp := computeResponseDTO{
		RequestID: rc.ID, Family: out.Family, Width: out.Width, Status: "ok",
		Prefixes: out.Prefixes, Empty: out.EmptyResult, Version: Version,
		Warnings: out.Warnings,
		Proof: proofDTO{
			Width:              out.Proof.Width,
			TargetAddressCount: out.Proof.TargetAddressCount,
			CoverAddressCount:  out.Proof.CoverAddressCount,
			BlockCount:         out.Proof.BlockCount,
			ExactlyEquivalent:  out.Proof.Equivalent,
			Overlaps:           out.Proof.Overlaps,
			MergeableSiblings:  out.Proof.SiblingMerges,
		},
	}
	for _, st := range out.Steps {
		resp.Steps = append(resp.Steps, stepDTO{
			Name: st.Name, Detail: st.Detail, DurationUS: st.DurationUS,
			Location: "internal/netmodel/engine.go:Compute",
		})
	}

	rec := store.Record{
		RequestID: rc.ID, Family: out.Family, Width: out.Width, Status: "ok",
		ResultJSON: mustJSON(out.Prefixes), Warnings: mustJSON(out.Warnings),
		StepsJSON: mustJSON(resp.Steps), ClientRef: rc.ClientRef, CreatedAt: created,
	}
	s.persist(r.Context(), rc, dto, rec)

	s.log.Log(LogEntry{
		Time: s.now(), Level: "info", RequestID: rc.ID, ClientRef: rc.ClientRef,
		Message: "compute_ok", PrefixCount: len(out.Prefixes),
		TargetAddresses: out.Proof.TargetAddressCount,
		Equivalent:      out.Proof.Equivalent, Warnings: out.Warnings,
		Steps: stepNames(out.Steps), Version: Version,
	})
	writeJSON(w, http.StatusOK, resp)
}

func (s *Service) persist(ctx context.Context, rc requestContext, dto computeRequestDTO, rec store.Record) {
	rec.AllowJSON = mustJSON(dto.Allow)
	rec.ExclJSON = mustJSON(dto.Exclude)
	if rec.Family == "" {
		rec.Family = strings.ToLower(dto.Family)
	}
	if rec.Width == 0 {
		rec.Width = 0
	}
	if err := s.store.Save(ctx, rec); err != nil {
		s.log.Log(LogEntry{
			Time: s.now(), Level: "error", RequestID: rc.ID, Message: "persist_failed",
			Error: err.Error(), Location: callerLocation(2), Version: Version,
		})
	}
}

func (s *Service) handleGet(w http.ResponseWriter, r *http.Request) {
	rc := identity(r)
	id := r.PathValue("id")
	rec, err := s.store.Get(r.Context(), id)
	if errors.Is(err, store.ErrNotFound) {
		s.fail(w, r, http.StatusNotFound, "not_found", err)
		return
	}
	if err != nil {
		s.fail(w, r, http.StatusInternalServerError, "store_error", err)
		return
	}
	writeJSON(w, http.StatusOK, recordToJSON(rec, rc.ID))
}

func (s *Service) handleList(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := parseClampedInt(q.Get("limit"), 50, 1, 500)
	offset := parseClampedInt(q.Get("offset"), 0, 0, 1_000_000)
	family := q.Get("family")
	status := q.Get("status")
	if status != "" && status != "ok" && status != "error" {
		s.fail(w, r, http.StatusBadRequest, "invalid_status_filter",
			errors.New("status must be 'ok' or 'error'"))
		return
	}
	recs, err := s.store.List(r.Context(), limit, offset, family, status)
	if err != nil {
		s.fail(w, r, http.StatusInternalServerError, "store_error", err)
		return
	}
	out := make([]json.RawMessage, 0, len(recs))
	for i := range recs {
		out = append(out, recordToJSON(&recs[i], identity(r).ID))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": identity(r).ID, "count": len(out), "records": out,
	})
}

func (s *Service) handleStats(w http.ResponseWriter, r *http.Request) {
	counts, err := s.store.CountByStatus(r.Context())
	if err != nil {
		s.fail(w, r, http.StatusInternalServerError, "store_error", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": identity(r).ID, "status_counts": counts, "version": Version,
	})
}

func (s *Service) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "version": Version})
}

// ---- helpers --------------------------------------------------------------

func (s *Service) fail(w http.ResponseWriter, r *http.Request, status int, code string, err error) {
	rc := identity(r)
	s.log.Log(LogEntry{
		Time: s.now(), Level: "error", RequestID: rc.ID, ClientRef: rc.ClientRef,
		Message: "http_error", ErrorCode: code, Error: err.Error(),
		Location: callerLocation(2), Version: Version,
	})
	writeJSON(w, status, errorDTO{
		RequestID: rc.ID, Status: "error", ErrorCode: code, Error: err.Error(),
		Location: callerLocation(2), Version: Version,
	})
}

func httpStatusFor(code string) int {
	switch code {
	case netmodel.KindMalformed, netmodel.KindPrefixTooLong, netmodel.KindHostBits,
		netmodel.KindFamilyMismatch:
		return http.StatusBadRequest
	case "verification_failure":
		// Internal uncertainty: the post-check disproved the answer. Never 2xx.
		return http.StatusInternalServerError
	default:
		return http.StatusBadRequest
	}
}

func callerLocation(skip int) string {
	_, file, line, ok := runtime.Caller(skip)
	if !ok {
		return "unknown"
	}
	// Keep it short and module-relative (internal/... or cmd/...), regardless
	// of the absolute checkout path.
	for _, marker := range []string{"/internal/", "/cmd/"} {
		if i := strings.Index(file, marker); i >= 0 {
			file = file[i+1:]
			break
		}
	}
	return fmt.Sprintf("%s:%d", file, line)
}

func stepNames(steps []netmodel.Step) []string {
	out := make([]string, len(steps))
	for i, s := range steps {
		out[i] = s.Name
	}
	return out
}

func recordToJSON(rec *store.Record, corrID string) json.RawMessage {
	var prefixes []string
	_ = json.Unmarshal([]byte(rec.ResultJSON), &prefixes)
	if prefixes == nil {
		prefixes = []string{}
	}
	m := map[string]any{
		"request_id":  rec.RequestID,
		"correlation": corrID,
		"family":      rec.Family,
		"width":       rec.Width,
		"status":      rec.Status,
		"prefixes":    prefixes,
		"allow":       json.RawMessage(orEmptyArray(rec.AllowJSON)),
		"exclude":     json.RawMessage(orEmptyArray(rec.ExclJSON)),
		"client_ref":  rec.ClientRef,
		"created_at":  rec.CreatedAt.Format(time.RFC3339Nano),
	}
	if rec.Status == "error" {
		m["error_code"] = rec.ErrorCode
		m["error"] = rec.ErrorText
	}
	var warns []string
	if rec.Warnings != "" {
		_ = json.Unmarshal([]byte(rec.Warnings), &warns)
	}
	m["warnings"] = warns
	if rec.StepsJSON != "" && rec.StepsJSON != "[]" {
		m["steps"] = json.RawMessage(rec.StepsJSON)
	}
	raw, _ := json.Marshal(m)
	return raw
}

func orEmptyArray(s string) string {
	if strings.TrimSpace(s) == "" {
		return "[]"
	}
	return s
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "[]"
	}
	return string(b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func parseClampedInt(s string, def, lo, hi int) int {
	if s == "" {
		return def
	}
	n := def
	if _, err := fmt.Sscanf(s, "%d", &n); err != nil {
		return def
	}
	if n < lo {
		return lo
	}
	if n > hi {
		return hi
	}
	return n
}
