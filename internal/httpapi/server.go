// Package httpapi exposes the broker over HTTP using only the standard
// library. Typed kernel errors map to distinct status codes and JSON error
// codes so clients and tests can assert the exact failure category.
package httpapi

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"localbroker/internal/broker"
	"localbroker/internal/kernel"
	"localbroker/internal/protocol"
	"localbroker/internal/replay"
)

// Server wires the broker to net/http.
type Server struct {
	b   *broker.Broker
	mux *http.ServeMux
}

// NewServer builds the HTTP handler.
func NewServer(b *broker.Broker) *Server {
	s := &Server{b: b, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.health)
	s.mux.HandleFunc("POST /v1/queues", s.createQueue)
	s.mux.HandleFunc("POST /v1/queues/{queue}/messages", s.publish)
	s.mux.HandleFunc("POST /v1/queues/{queue}/claims", s.claim)
	s.mux.HandleFunc("POST /v1/queues/{queue}/receipts/{receipt}/extend", s.extend)
	s.mux.HandleFunc("POST /v1/queues/{queue}/receipts/{receipt}/ack", s.ack)
	s.mux.HandleFunc("POST /v1/queues/{queue}/receipts/{receipt}/nack", s.nack)
	s.mux.HandleFunc("GET /v1/queues/{queue}/messages/{id}", s.getMessage)
	s.mux.HandleFunc("GET /v1/queues/{queue}/dead", s.listDead)
	s.mux.HandleFunc("POST /v1/queues/{queue}/expire", s.expire)
	s.mux.HandleFunc("GET /v1/queues/{queue}/replay", s.replayView)
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "version": protocol.Version, "run_id": s.b.RunID()})
}

type createQueueReq struct {
	Name              string `json:"name"`
	VisibilityTimeout string `json:"visibility_timeout"`
	MaxAttempts       int    `json:"max_attempts"`
}

func (s *Server) createQueue(w http.ResponseWriter, r *http.Request) {
	var req createQueueReq
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	vis, err := time.ParseDuration(req.VisibilityTimeout)
	if err != nil {
		writeError(w, r, http.StatusBadRequest, "invalid_visibility_timeout", "visibility_timeout must be a Go duration, e.g. 2s")
		return
	}
	cfg := protocol.QueueConfig{Name: req.Name, VisibilityTimeout: vis, MaxAttempts: req.MaxAttempts}
	if err := s.b.CreateQueue(r.Context(), cfg); err != nil {
		writeBrokerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"name": cfg.Name, "visibility_timeout": cfg.VisibilityTimeout.String(), "max_attempts": cfg.MaxAttempts})
}

func (s *Server) publish(w http.ResponseWriter, r *http.Request) {
	queue := r.PathValue("queue")
	body := []byte{}
	if strings.Contains(r.Header.Get("Content-Type"), "application/json") {
		var req struct {
			BodyB64 string `json:"body_base64"`
			Body    string `json:"body"`
		}
		if err := decodeJSON(r, &req); err != nil {
			writeError(w, r, http.StatusBadRequest, "invalid_json", err.Error())
			return
		}
		if req.BodyB64 != "" {
			raw, err := base64.StdEncoding.DecodeString(req.BodyB64)
			if err != nil {
				writeError(w, r, http.StatusBadRequest, "invalid_body_base64", err.Error())
				return
			}
			body = raw
		} else {
			body = []byte(req.Body)
		}
	} else {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			writeError(w, r, http.StatusBadRequest, "invalid_body", err.Error())
			return
		}
		body = raw
	}
	pub, err := s.b.Publish(r.Context(), queue, body)
	if err != nil {
		writeBrokerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"id": pub.ID})
}

type claimResp struct {
	ID         string `json:"id"`
	BodyBase64 string `json:"body_base64"`
	Attempts   int    `json:"attempts"`
	Receipt    string `json:"receipt"`
	ReceiptGen int64  `json:"receipt_gen"`
	Deadline   string `json:"deadline"`
}

func (s *Server) claim(w http.ResponseWriter, r *http.Request) {
	queue := r.PathValue("queue")
	d, err := s.b.Claim(r.Context(), queue)
	if err != nil {
		writeBrokerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, claimResp{
		ID:         d.MessageID,
		BodyBase64: base64.StdEncoding.EncodeToString(d.Body),
		Attempts:   d.Attempts,
		Receipt:    d.Receipt,
		ReceiptGen: d.ReceiptGen,
		Deadline:   d.Deadline.Format(time.RFC3339Nano),
	})
}

func (s *Server) extend(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Extra string `json:"extra"`
	}
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, r, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	extra, err := time.ParseDuration(req.Extra)
	if err != nil {
		writeError(w, r, http.StatusBadRequest, "invalid_extra", "extra must be a Go duration, e.g. 10s")
		return
	}
	dl, err := s.b.Extend(r.Context(), r.PathValue("queue"), r.PathValue("receipt"), extra)
	if err != nil {
		writeBrokerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deadline": dl.Format(time.RFC3339Nano)})
}

func (s *Server) ack(w http.ResponseWriter, r *http.Request) {
	if err := s.b.Ack(r.Context(), r.PathValue("queue"), r.PathValue("receipt")); err != nil {
		writeBrokerError(w, r, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) nack(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Reason string `json:"reason"`
	}
	_ = decodeJSON(r, &req) // body optional
	if err := s.b.Nack(r.Context(), r.PathValue("queue"), r.PathValue("receipt"), req.Reason); err != nil {
		writeBrokerError(w, r, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func messageJSON(m protocol.Message) map[string]any {
	fails := make([]map[string]any, 0, len(m.Failures))
	for _, f := range m.Failures {
		fails = append(fails, map[string]any{
			"attempt": f.Attempt, "kind": string(f.Kind), "reason": f.Reason,
			"at": f.At.Format(time.RFC3339Nano),
			"deadline": f.Deadline.Format(time.RFC3339Nano),
		})
	}
	return map[string]any{
		"id": m.ID, "queue": m.Queue, "state": string(m.State),
		"attempts": m.Attempts, "receipt_gen": m.ReceiptGen,
		"deadline":    m.Deadline.Format(time.RFC3339Nano),
		"enqueued_at": m.EnqueuedAt.Format(time.RFC3339Nano),
		"failures":    fails,
	}
}

func (s *Server) getMessage(w http.ResponseWriter, r *http.Request) {
	m, err := s.b.Message(r.Context(), r.PathValue("queue"), r.PathValue("id"))
	if err != nil {
		writeBrokerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, messageJSON(m))
}

func (s *Server) listDead(w http.ResponseWriter, r *http.Request) {
	dead, err := s.b.Dead(r.Context(), r.PathValue("queue"))
	if err != nil {
		writeBrokerError(w, r, err)
		return
	}
	out := make([]map[string]any, 0, len(dead))
	for _, m := range dead {
		j := messageJSON(m)
		j["body_base64"] = base64.StdEncoding.EncodeToString(m.Body)
		out = append(out, j)
	}
	writeJSON(w, http.StatusOK, map[string]any{"dead": out, "count": len(out)})
}

func (s *Server) expire(w http.ResponseWriter, r *http.Request) {
	n, err := s.b.ExpireDue(r.Context(), r.PathValue("queue"))
	if err != nil {
		writeBrokerError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]int{"expired": n})
}

func (s *Server) replayView(w http.ResponseWriter, r *http.Request) {
	queue := r.PathValue("queue")
	// The HTTP server reaches the store through the broker; replay reads only
	// the event log, never live state.
	res, err := replay.FromStore(r.Context(), s.b.Store(), queue)
	if err != nil {
		writeBrokerError(w, r, err)
		return
	}
	msgs := map[string]any{}
	for id, m := range res.Messages {
		fails := make([]string, 0, len(m.Failures))
		for _, f := range m.Failures {
			fails = append(fails, string(f.Kind))
		}
		msgs[id] = map[string]any{
			"state": string(m.State), "attempts": m.Attempts,
			"receipt_gen": m.ReceiptGen, "failure_kinds": fails, "last_seq": m.LastSeq,
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"event_count": res.EventCount, "messages": msgs})
}

// ---- helpers ----

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, r *http.Request, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": code, "message": msg, "path": r.URL.Path})
}

// writeBrokerError maps the closed kernel error taxonomy to HTTP status codes.
// Unknown errors are reported as 500 internal_error, never as success.
func writeBrokerError(w http.ResponseWriter, r *http.Request, err error) {
	var ia *kernel.InvalidArgument
	switch {
	case errors.As(err, &ia):
		writeError(w, r, http.StatusBadRequest, "invalid_argument", ia.Error())
	case errors.Is(err, kernel.ErrQueueNotFound):
		writeError(w, r, http.StatusNotFound, "queue_not_found", err.Error())
	case errors.Is(err, kernel.ErrNoMessage):
		writeError(w, r, http.StatusNotFound, "no_message", err.Error())
	case errors.Is(err, kernel.ErrQueueExists):
		writeError(w, r, http.StatusConflict, "queue_exists", err.Error())
	case errors.Is(err, kernel.ErrInvalidReceipt):
		writeError(w, r, http.StatusBadRequest, "invalid_receipt", err.Error())
	case errors.Is(err, kernel.ErrStaleReceipt):
		writeError(w, r, http.StatusConflict, "stale_receipt", err.Error())
	case errors.Is(err, kernel.ErrLeaseExpired):
		writeError(w, r, http.StatusGone, "lease_expired", err.Error())
	case errors.Is(err, kernel.ErrMessageDead):
		writeError(w, r, http.StatusGone, "message_dead", err.Error())
	case errors.Is(err, kernel.ErrAlreadyAcked):
		writeError(w, r, http.StatusConflict, "already_acked", err.Error())
	case errors.Is(err, kernel.ErrNotInvisible):
		writeError(w, r, http.StatusConflict, "not_invisible", err.Error())
	case errors.Is(err, store.ErrClosed):
		writeError(w, r, http.StatusServiceUnavailable, "store_closed", err.Error())
	case errors.Is(err, kernel.ErrUnknownState):
		writeError(w, r, http.StatusInternalServerError, "unknown_state", err.Error())
	default:
		writeError(w, r, http.StatusInternalServerError, "internal_error", err.Error())
	}
}
