// Package api exposes the cover service over HTTP. All responses share one
// envelope that carries the request id, versions, data and separated
// failures/advisories, so every result can be correlated with logs and the
// persisted replay record.
package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"cidrcov/internal/engine"
	"cidrcov/internal/store"
)

// Server wires the engine, store and logger into HTTP handlers.
type Server struct {
	EngineOpts engine.Options
	Store      *store.Store
	Logger     *slog.Logger
	Now        func() time.Time
}

// Envelope is the common shape of every response.
type Envelope struct {
	RequestID string          `json:"request_id"`
	Timestamp string          `json:"timestamp"`
	Versions  Versions        `json:"versions"`
	Data      json.RawMessage `json:"data,omitempty"`
	Error     *APIError       `json:"error,omitempty"`
}

// Versions identifies the processing code that produced a response.
type Versions struct {
	Service   string `json:"service"`
	Algorithm string `json:"algorithm"`
}

// APIError is one externally visible failure. Uncertain-but-accepted results
// are NOT errors; they live in data.advisories.
type APIError struct {
	Code    string          `json:"code"`
	Message string          `json:"message"`
	Detail  json.RawMessage `json:"detail,omitempty"`
}

// CoverRequest is the POST /v1/cover payload. Unknown fields are rejected.
type CoverRequest struct {
	// RequestID lets callers assign their own correlation id; empty => server
	// generates one. Re-using an id returns 409 instead of overwriting.
	RequestID string   `json:"request_id,omitempty"`
	Allow     []string `json:"allow"`
	Exclude   []string `json:"exclude,omitempty"`
}

const maxBodyBytes = 1 << 20 // 1 MiB

// Routes builds the HTTP mux.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /version", s.handleVersion)
	mux.HandleFunc("POST /v1/cover", s.handleCover)
	mux.HandleFunc("GET /v1/requests/{id}", s.handleGet)
	mux.HandleFunc("POST /v1/replay/{id}", s.handleReplay)
	mux.HandleFunc("GET /v1/requests", s.handleList)
	return s.recoverer(s.requestIDMiddleware(mux))
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now().UTC()
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleVersion(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, Versions{
		Service: engine.ServiceVersion, Algorithm: engine.AlgorithmVersion,
	})
}

func (s *Server) handleCover(w http.ResponseWriter, r *http.Request) {
	reqID := requestIDFromContext(r)
	logger := s.Logger.With("request_id", reqID, "endpoint", "POST /v1/cover")

	var req CoverRequest
	if err := decodeStrict(r, &req); err != nil {
		logger.Warn("malformed request body", "reason", err.Error())
		writeEnvelope(w, http.StatusBadRequest, reqID, s.now(), nil, &APIError{
			Code: "malformed_request", Message: err.Error(),
		})
		return
	}
	// A body-supplied id overrides the header/generated one and is validated
	// before any work happens.
	if strings.TrimSpace(req.RequestID) != "" {
		reqID = strings.TrimSpace(req.RequestID)
		logger = logger.With("assigned_request_id", reqID)
		w.Header().Set("X-Request-ID", reqID)
	}
	if !validID(reqID) {
		writeEnvelope(w, http.StatusBadRequest, reqID, s.now(), nil, &APIError{
			Code:    "invalid_request_id",
			Message: "request_id must match [A-Za-z0-9._-]{1,128}",
		})
		return
	}
	// nil arrays are treated as empty lists but empty allow is valid (=> empty cover).
	result := engine.Compute(defaultStrings(req.Allow), defaultStrings(req.Exclude), s.EngineOpts)

	resultJSON, _ := json.Marshal(result)
	allowJSON, _ := json.Marshal(req.Allow)
	excludeJSON, _ := json.Marshal(req.Exclude)
	rec := store.Record{
		RequestID:    reqID,
		CreatedAt:    s.now(),
		AllowInput:   string(allowJSON),
		ExcludeInput: string(excludeJSON),
		Status:       result.Status,
		ResultJSON:   string(resultJSON),
	}
	saveErr := s.Store.Save(r.Context(), rec)
	if saveErr != nil {
		if errors.Is(saveErr, store.ErrDuplicate) {
			logger.Warn("duplicate request id", "reason", saveErr.Error())
			writeEnvelope(w, http.StatusConflict, reqID, s.now(), nil, &APIError{
				Code: "duplicate_request_id", Message: saveErr.Error(),
			})
			return
		}
		logger.Error("persist failed", "reason", saveErr.Error())
		writeEnvelope(w, http.StatusInternalServerError, reqID, s.now(), nil, &APIError{
			Code: "persist_failed", Message: saveErr.Error(),
		})
		return
	}

	status := http.StatusOK
	if result.Status == "error" {
		status = http.StatusUnprocessableEntity
	}
	logger.Info("cover computed",
		"status", result.Status,
		"ipv4_prefixes", result.V4PrefixCount,
		"ipv6_prefixes", result.V6PrefixCount,
		"total_covered", result.TotalCovered,
		"advisories", len(result.Advisories),
		"failures", len(result.Failures))
	writeEnvelope(w, status, reqID, s.now(), result, nil)
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	rec, err := s.Store.Get(r.Context(), id)
	if err != nil {
		code := http.StatusNotFound
		apiErr := &APIError{Code: "not_found", Message: err.Error()}
		if !errors.Is(err, store.ErrNotFound) {
			code = http.StatusInternalServerError
			apiErr = &APIError{Code: "store_error", Message: err.Error()}
		}
		writeEnvelope(w, code, requestIDFromContext(r), s.now(), nil, apiErr)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"request_id": rec.RequestID,
		"created_at": rec.CreatedAt.Format(time.RFC3339Nano),
		"allow":      json.RawMessage(rec.AllowInput),
		"exclude":    json.RawMessage(rec.ExcludeInput),
		"status":     rec.Status,
		"result":     json.RawMessage(rec.ResultJSON),
	})
}

// handleReplay re-runs the ORIGINAL stored inputs through the current engine
// and reports whether the result is byte-identical to the recorded one. It
// never mutates stored state.
func (s *Server) handleReplay(w http.ResponseWriter, r *http.Request) {
	reqID := requestIDFromContext(r)
	id := r.PathValue("id")
	logger := s.Logger.With("request_id", reqID, "replay_of", id)
	rec, err := s.Store.Get(r.Context(), id)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeEnvelope(w, http.StatusNotFound, reqID, s.now(), nil,
				&APIError{Code: "not_found", Message: err.Error()})
			return
		}
		writeEnvelope(w, http.StatusInternalServerError, reqID, s.now(), nil,
			&APIError{Code: "store_error", Message: err.Error()})
		return
	}
	var allow, exclude []string
	_ = json.Unmarshal([]byte(rec.AllowInput), &allow)
	_ = json.Unmarshal([]byte(rec.ExcludeInput), &exclude)
	fresh := engine.Compute(allow, exclude, s.EngineOpts)
	freshJSON, _ := json.Marshal(fresh)

	replay := map[string]any{
		"replayed_request_id": rec.RequestID,
		"algorithm":           engine.AlgorithmVersion,
		"matches_recorded":    string(freshJSON) == rec.ResultJSON,
		"recorded_result":     json.RawMessage(rec.ResultJSON),
		"fresh_result":        fresh,
	}
	logger.Info("replay executed", "matches", replay["matches_recorded"])
	writeEnvelope(w, http.StatusOK, reqID, s.now(), replay, nil)
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	recs, err := s.Store.Recent(r.Context(), 50)
	if err != nil {
		writeEnvelope(w, http.StatusInternalServerError, requestIDFromContext(r), s.now(), nil,
			&APIError{Code: "store_error", Message: err.Error()})
		return
	}
	type item struct {
		RequestID string `json:"request_id"`
		CreatedAt string `json:"created_at"`
		Status    string `json:"status"`
	}
	out := make([]item, 0, len(recs))
	for _, rec := range recs {
		out = append(out, item{rec.RequestID, rec.CreatedAt.Format(time.RFC3339Nano), rec.Status})
	}
	writeJSON(w, http.StatusOK, map[string]any{"requests": out})
}

// ---- middleware / helpers -------------------------------------------------

type ctxKey string

const reqIDKey ctxKey = "request_id"

func (s *Server) requestIDMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimSpace(r.Header.Get("X-Request-ID"))
		if id == "" {
			id = newRequestID()
		}
		ctx := context.WithValue(r.Context(), reqIDKey, id)
		w.Header().Set("X-Request-ID", id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func (s *Server) recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				s.Logger.Error("panic recovered", "request_id", requestIDFromContext(r),
					"panic", rec)
				writeEnvelope(w, http.StatusInternalServerError, requestIDFromContext(r),
					s.now(), nil, &APIError{Code: "internal_panic", Message: "internal server error"})
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func requestIDFromContext(r *http.Request) string {
	if v, ok := r.Context().Value(reqIDKey).(string); ok {
		return v
	}
	return "unknown"
}

func newRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "req-fallback-" + time.Now().Format("20060102T150405.000000000")
	}
	return "req-" + hex.EncodeToString(b[:])
}

func validID(id string) bool {
	if len(id) < 1 || len(id) > 128 {
		return false
	}
	for _, c := range id {
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9',
			c == '.', c == '_', c == '-':
		default:
			return false
		}
	}
	return true
}

func defaultStrings(in []string) []string {
	if in == nil {
		return []string{}
	}
	return in
}

func decodeStrict(r *http.Request, dst any) error {
	if r.Body == nil {
		return errors.New("empty request body")
	}
	dec := json.NewDecoder(io.LimitReader(r.Body, maxBodyBytes+1))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		if errors.Is(err, io.EOF) {
			return errors.New("empty request body")
		}
		return err
	}
	// Reject trailing data beyond the single JSON object.
	var extra json.RawMessage
	if err := dec.Decode(&extra); err != io.EOF {
		return errors.New("body must contain exactly one JSON object")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeEnvelope(w http.ResponseWriter, status int, reqID string, ts time.Time, data any, apiErr *APIError) {
	env := Envelope{
		RequestID: reqID,
		Timestamp: ts.Format(time.RFC3339Nano),
		Versions:  Versions{Service: engine.ServiceVersion, Algorithm: engine.AlgorithmVersion},
		Error:     apiErr,
	}
	if data != nil {
		raw, err := json.Marshal(data)
		if err != nil {
			status = http.StatusInternalServerError
			env.Error = &APIError{Code: "response_encode_failed", Message: err.Error()}
			env.Data = nil
		} else {
			env.Data = raw
		}
	}
	writeJSON(w, status, env)
}
