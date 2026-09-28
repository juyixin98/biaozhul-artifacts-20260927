// Package httpapi exposes the broker over plain net/http JSON. Every error
// response carries the concrete failure class ("code") so clients and tests
// can branch on it; internal/unknown states are 500 with a correlation id,
// never a 200.
package httpapi

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strings"

	"workbroker/internal/broker"
	"workbroker/internal/kernel"
	"workbroker/internal/protocol"
	"workbroker/internal/version"
)

// Handler wires HTTP routes to a *broker.Broker.
type Handler struct {
	b   *broker.Broker
	log *log.Logger
}

// NewHandler builds the handler and a ServeMux exposing the routes.
func NewHandler(b *broker.Broker, logger *log.Logger) http.Handler {
	if logger == nil {
		logger = log.Default()
	}
	h := &Handler{b: b, log: logger}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /version", h.version)
	mux.HandleFunc("POST /v1/partitions/{partition}/messages", h.send)
	mux.HandleFunc("POST /v1/partitions/{partition}/receives", h.receive)
	mux.HandleFunc("POST /v1/receipts/{receipt}/extend", h.extend)
	mux.HandleFunc("POST /v1/receipts/{receipt}/ack", h.ack)
	mux.HandleFunc("POST /v1/receipts/{receipt}/nack", h.nack)
	mux.HandleFunc("GET /v1/partitions/{partition}/dead", h.deadList)
	mux.HandleFunc("GET /replay/events", h.events)
	mux.HandleFunc("GET /replay/snapshot", h.snapshot)
	return mux
}

func (h *Handler) version(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"service":        version.Service,
		"proto_version":  version.ProtocolVersion,
		"schema_version": version.SchemaVersion,
		"limits": map[string]any{
			"min_visibility_seconds": int64(kernel.MinVisibility.Seconds()),
			"max_visibility_seconds": int64(kernel.MaxVisibility.Seconds()),
			"default_max_attempts":   kernel.DefaultMaxAttempts,
		},
	})
}

type sendRequest struct {
	Body         []byte `json:"body"`
	MaxAttempts  int64  `json:"max_attempts"`
	DelaySeconds int64  `json:"delay_seconds"`
}

func (h *Handler) send(w http.ResponseWriter, r *http.Request) {
	partition := r.PathValue("partition")
	var req sendRequest
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	msg, err := h.b.Send(r.Context(), broker.SendInput{
		Partition:    partition,
		Body:         req.Body,
		MaxAttempts:  req.MaxAttempts,
		DelaySeconds: req.DelaySeconds,
	})
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id":           msg.ID,
		"partition":    msg.Partition,
		"max_attempts": msg.MaxAttempts,
		"available_at": msg.AvailableAt,
		"created_at":   msg.CreatedAt,
	})
}

type receiveRequest struct {
	WorkerID          string `json:"worker_id"`
	VisibilitySeconds int64  `json:"visibility_seconds"`
	WaitSeconds       int64  `json:"wait_seconds"`
}

func (h *Handler) receive(w http.ResponseWriter, r *http.Request) {
	partition := r.PathValue("partition")
	var req receiveRequest
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	got, err := h.b.Receive(r.Context(), partition, req.WorkerID,
		req.VisibilitySeconds, req.WaitSeconds)
	if err != nil {
		writeError(w, r, err)
		return
	}
	type item struct {
		ID              string `json:"id"`
		ReceiptID       string `json:"receipt_id"`
		Body            []byte `json:"body"`
		Attempts        int64  `json:"attempts"`
		ReceivedAt      string `json:"received_at"`
		VisibilityUntil string `json:"visibility_until"`
	}
	items := make([]item, 0, len(got))
	for _, d := range got {
		items = append(items, item{
			ID: d.ID, ReceiptID: d.ReceiptID, Body: d.Body, Attempts: d.Attempts,
			ReceivedAt:      d.ReceivedAt.UTC().Format("2006-01-02T15:04:05.999999999Z07:00"),
			VisibilityUntil: d.VisibilityUntil.UTC().Format("2006-01-02T15:04:05.999999999Z07:00"),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"messages": items})
}

type extendRequest struct {
	ExtendSeconds int64 `json:"extend_seconds"`
}

func (h *Handler) extend(w http.ResponseWriter, r *http.Request) {
	receipt := r.PathValue("receipt")
	var req extendRequest
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	until, err := h.b.Extend(r.Context(), receipt, req.ExtendSeconds)
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"receipt_id":       receipt,
		"visibility_until": until,
	})
}

func (h *Handler) ack(w http.ResponseWriter, r *http.Request) {
	receipt := r.PathValue("receipt")
	if err := h.b.Ack(r.Context(), receipt); err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"receipt_id": receipt, "status": "acked"})
}

type nackRequest struct {
	Reason string `json:"reason"`
}

func (h *Handler) nack(w http.ResponseWriter, r *http.Request) {
	receipt := r.PathValue("receipt")
	var req nackRequest
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	if err := h.b.Nack(r.Context(), receipt, strings.TrimSpace(req.Reason)); err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"receipt_id": receipt, "status": "nacked"})
}

func (h *Handler) deadList(w http.ResponseWriter, r *http.Request) {
	partition := r.PathValue("partition")
	got, err := h.b.DeadList(r.Context(), partition, 100)
	if err != nil {
		writeError(w, r, err)
		return
	}
	if got == nil {
		got = []broker.DeadView{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"dead": got})
}

func (h *Handler) events(w http.ResponseWriter, r *http.Request) {
	after := int64(0)
	limit := int64(1000)
	if v := r.URL.Query().Get("after_seq"); v != "" {
		if !parseNonNegInt(v, &after) {
			writeError(w, r, protocol.NewFailure("events", protocol.FailValidation,
				"after_seq must be a non-negative integer", nil))
			return
		}
	}
	if v := r.URL.Query().Get("limit"); v != "" {
		if !parseNonNegInt(v, &limit) {
			writeError(w, r, protocol.NewFailure("events", protocol.FailValidation,
				"limit must be a non-negative integer", nil))
			return
		}
	}
	evs, err := h.b.Store().Events(r.Context(), after, limit)
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": evs, "last_seq": lastSeq(evs, after)})
}

func (h *Handler) snapshot(w http.ResponseWriter, r *http.Request) {
	snap, err := h.b.ReplaySnapshot(r.Context())
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"last_seq": snap.LastSeq,
		"messages": mapValues(snap.Messages),
		"dead":     snap.Dead,
		"receipts": mapLen(snap.Receipts),
	})
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func decodeBody(w http.ResponseWriter, r *http.Request, v any) error {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return protocol.NewFailure("http", protocol.FailValidation,
			"invalid JSON body: "+err.Error(), err)
	}
	return nil
}

func parseNonNegInt(s string, dst *int64) bool {
	var n int64
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
		n = n*10 + int64(c-'0')
	}
	*dst = n
	return true
}

func lastSeq(evs []protocol.Event, fallback int64) int64 {
	if len(evs) > 0 {
		return evs[len(evs)-1].Seq
	}
	return fallback
}

func mapValues[V any](m map[string]V) []V {
	out := make([]V, 0, len(m))
	for _, v := range m {
		out = append(out, v)
	}
	return out
}

func mapLen[V any](m map[string]V) int { return len(m) }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	if err := enc.Encode(v); err != nil && !errors.Is(err, http.ErrHandlerTimeout) {
		log.Printf("httpapi: write response failed: %v", err)
	}
}

// statusFor maps a FailureClass to an HTTP status.
func statusFor(class protocol.FailureClass) int {
	switch class {
	case protocol.FailValidation:
		return http.StatusBadRequest
	case protocol.FailReceiptNotFound, protocol.FailMessageNotFound:
		return http.StatusNotFound
	case protocol.FailReceiptConsumed, protocol.FailReceiptExpired, protocol.FailReceiptStale:
		return http.StatusConflict
	case protocol.FailMessageDead:
		return http.StatusGone
	case protocol.FailTimeout:
		return http.StatusRequestTimeout
	case protocol.FailConflict:
		return http.StatusConflict
	default:
		return http.StatusInternalServerError
	}
}

func writeError(w http.ResponseWriter, r *http.Request, err error) {
	f := protocol.AsFailure(err)
	if f == nil {
		f = protocol.NewFailure("http", protocol.FailInternal, err.Error(), err)
	}
	status := statusFor(f.Class)
	if status >= 500 {
		log.Printf("httpapi: %s %s -> 500 class=%s detail=%s",
			r.Method, r.URL.Path, f.Class, f.Detail)
	}
	writeJSON(w, status, map[string]any{
		"error": map[string]any{
			"class":  string(f.Class),
			"op":     f.Op,
			"detail": f.Detail,
		},
	})
}
