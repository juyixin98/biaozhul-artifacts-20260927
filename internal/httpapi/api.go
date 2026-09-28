// Package httpapi exposes the control plane (subscription snapshots) and the
// data plane (publish, decision log, deterministic replay) over plain net/http
// JSON. Every response carries the request id so diagnostics can be tied to a
// single API call.
package httpapi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"topicrouter/internal/diag"
	"topicrouter/internal/router"
	"topicrouter/internal/store"
	"topicrouter/internal/topic"
)

// Server wires the router and logger into HTTP handlers.
type Server struct {
	rt  *router.Router
	log *diag.Logger
}

// NewServer builds an http.Handler with all routes registered.
func NewServer(rt *router.Router, lg *diag.Logger) http.Handler {
	s := &Server{rt: rt, log: lg}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /v1/version", s.getVersion)
	mux.HandleFunc("GET /v1/subscriptions", s.listSubs)
	mux.HandleFunc("GET /v1/subscriptions/{id}", s.getSub)
	mux.HandleFunc("PUT /v1/subscriptions/{id}", s.putSub)
	mux.HandleFunc("DELETE /v1/subscriptions/{id}", s.deleteSub)
	mux.HandleFunc("POST /v1/publish", s.publish)
	mux.HandleFunc("GET /v1/decisions", s.listDecisions)
	mux.HandleFunc("GET /v1/decisions/{messageID}", s.getDecision)
	mux.HandleFunc("POST /v1/decisions/{messageID}/replay", s.replay)

	return s.requestID(s.recoverPanic(mux))
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) {
	v, err := s.rt.CurrentVersion(r.Context())
	if err != nil {
		s.writeUndecidable(w, r, "version", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]int64{"version": v})
}

type putSubscriptionReq struct {
	SubscriberID string `json:"subscriber_id"`
	Filter       string `json:"filter"`
	// ExpectedVersion: omit/-2 unconditional; -1 create-only; N optimistic.
	ExpectedVersion *int64 `json:"expected_version,omitempty"`
}

func (s *Server) putSub(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var req putSubscriptionReq
	if !s.decode(w, r, &req) {
		return
	}
	ev := int64(-2)
	if req.ExpectedVersion != nil {
		ev = *req.ExpectedVersion
	}
	// If-Match: "N" is an alternative optimistic-concurrency channel.
	if im := r.Header.Get("If-Match"); im != "" {
		n, err := strconv.ParseInt(strings.Trim(im, `"`), 10, 64)
		if err != nil {
			s.fail(w, r, http.StatusBadRequest, "MALFORMED_IF_MATCH",
				"If-Match must be an integer version", nil)
			return
		}
		ev = n
	}
	if req.SubscriberID == "" {
		s.fail(w, r, http.StatusBadRequest, "MISSING_SUBSCRIBER",
			"subscriber_id is required", map[string]any{"subscription_id": id})
		return
	}
	res, err := s.rt.Update(r.Context(), id, req.SubscriberID, req.Filter, false, ev)
	if err != nil {
		s.writeRouterError(w, r, err, "subscription_update")
		return
	}
	if res.Conflict {
		s.fail(w, r, http.StatusConflict, "VERSION_CONFLICT",
			"subscription changed; re-read and retry",
			map[string]any{"current_version": res.CurrentVersion})
		return
	}
	// Create vs update distinguished purely by whether the change advanced
	// version 1 (fresh id) — the store knows authoritatively via 200/201:
	// a first-time Put is 201; callers needing idempotency can use If-None-Match.
	status := http.StatusOK
	if res.Version == 1 {
		status = http.StatusCreated
	}
	writeJSON(w, status, map[string]any{
		"subscription_id": id,
		"version":         res.Version,
		"change":          string(res.Change),
		"request_id":      diag.RequestID(r.Context()),
	})
}

func (s *Server) deleteSub(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	res, err := s.rt.Update(r.Context(), id, "", "", true, -2)
	if err != nil {
		s.writeRouterError(w, r, err, "subscription_delete")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"subscription_id": id,
		"version":         res.Version,
		"change":          string(res.Change),
		"request_id":      diag.RequestID(r.Context()),
	})
}

func (s *Server) getSub(w http.ResponseWriter, r *http.Request) {
	sub, err := s.rt.Store().GetSubscription(r.Context(), r.PathValue("id"))
	if err != nil {
		var nf *store.NotFoundError
		if errors.As(err, &nf) {
			s.fail(w, r, http.StatusNotFound, "NOT_FOUND", "subscription not found",
				map[string]any{"id": nf.Key})
			return
		}
		s.writeUndecidable(w, r, "get_subscription", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"subscription_id": sub.ID,
		"subscriber_id":   sub.SubscriberID,
		"filter":          sub.Filter,
		"version":         sub.Version,
		"updated_at":      sub.UpdatedAt,
		"request_id":      diag.RequestID(r.Context()),
	})
}

func (s *Server) listSubs(w http.ResponseWriter, r *http.Request) {
	subs, err := s.rt.Store().ListSubscriptions(r.Context())
	if err != nil {
		s.writeUndecidable(w, r, "list_subscriptions", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"subscriptions": subs, "count": len(subs)})
}

type publishReq struct {
	MessageID string          `json:"message_id"`
	Topic     string          `json:"topic"`
	Payload   json.RawMessage `json:"payload,omitempty"`
}

func (s *Server) publish(w http.ResponseWriter, r *http.Request) {
	var req publishReq
	if !s.decode(w, r, &req) {
		return
	}
	// Payload is accepted verbatim and only its hash/redacted size is logged.
	payload := []byte(req.Payload)
	res, err := s.rt.Publish(r.Context(), router.Publication{
		MessageID: req.MessageID, Topic: req.Topic, Payload: payload,
	})
	if err != nil {
		s.writeRouterError(w, r, err, "publish")
		return
	}
	status := http.StatusOK
	if res.Inserted {
		status = http.StatusCreated
	}
	writeJSON(w, status, map[string]any{
		"message_id":        req.MessageID,
		"version":           res.Version,
		"matched":           res.SubIDs,
		"matched_count":     len(res.SubIDs),
		"duplicate":         !res.Inserted,
		"node_visits":       res.Stats.NodeVisits,
		"edge_lookups":      res.Stats.EdgeLookups,
		"terminals_hit":     res.Stats.TerminalsCollected,
		"dedup_hits":        res.Stats.DedupHits,
		"payload_sha256":    res.Decision.PayloadSHA,
		"request_id":        diag.RequestID(r.Context()),
	})
}

func (s *Server) getDecision(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("messageID")
	d, err := s.rt.HistoricDecision(r.Context(), id)
	if err != nil {
		var nf *store.NotFoundError
		if errors.As(err, &nf) {
			s.fail(w, r, http.StatusNotFound, "NOT_FOUND", "decision not found",
				map[string]any{"message_id": nf.Key})
			return
		}
		s.writeUndecidable(w, r, "get_decision", err)
		return
	}
	writeJSON(w, http.StatusOK, decisionJSON(d))
}

func (s *Server) listDecisions(w http.ResponseWriter, r *http.Request) {
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 || n > 500 {
			s.fail(w, r, http.StatusBadRequest, "MALFORMED_LIMIT",
				"limit must be 1..500", nil)
			return
		}
		limit = n
	}
	cursor := r.URL.Query().Get("cursor")
	page, err := s.rt.ListHistoric(r.Context(), limit, cursor)
	if err != nil {
		var nf *store.NotFoundError
		if errors.As(err, &nf) {
			s.fail(w, r, http.StatusBadRequest, "UNKNOWN_CURSOR",
				"cursor does not exist", map[string]any{"cursor": nf.Key})
			return
		}
		s.writeUndecidable(w, r, "list_decisions", err)
		return
	}
	out := make([]map[string]any, 0, len(page.Decisions))
	for _, d := range page.Decisions {
		out = append(out, decisionJSON(d))
	}
	resp := map[string]any{"decisions": out, "count": len(out), "has_more": page.HasMore}
	if page.NextCursor != "" {
		resp["next_cursor"] = page.NextCursor
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) replay(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("messageID")
	res, err := s.rt.Recompute(r.Context(), id)
	if err != nil {
		var nf *store.NotFoundError
		if errors.As(err, &nf) {
			s.fail(w, r, http.StatusNotFound, "NOT_FOUND", "decision not found",
				map[string]any{"message_id": nf.Key})
			return
		}
		s.writeUndecidable(w, r, "replay", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"message_id":    res.MessageID,
		"topic":         res.Topic,
		"bound_version": res.BoundVersion,
		"historic":      res.Historic,
		"recomputed":    res.Recomputed,
		"status":        res.Status, // match | mismatch
		"node_visits":   res.Stats.NodeVisits,
		"edge_lookups":  res.Stats.EdgeLookups,
		"request_id":    diag.RequestID(r.Context()),
	})
}

func decisionJSON(d store.Decision) map[string]any {
	return map[string]any{
		"message_id":   d.MessageID,
		"topic":        d.Topic,
		"version":      d.Version,
		"matched":      d.SubIDs,
		"stats":        d.Stats,
		"payload_sha":  d.PayloadSHA,
		"decided_at":   d.DecidedAt,
	}
}

// writeRouterError maps protocol/router rejections to 4xx categories and
// genuine failures to 503 (undecided: safe to retry).
func (s *Server) writeRouterError(w http.ResponseWriter, r *http.Request, err error, action string) {
	if pe, ok := topic.AsProtocolError(err); ok {
		s.fail(w, r, http.StatusBadRequest, string(pe.Class), pe.Detail,
			map[string]any{"action": action})
		return
	}
	var re *router.RejectError
	if errors.As(err, &re) {
		s.fail(w, r, http.StatusBadRequest, re.Class, re.Detail,
			map[string]any{"action": action})
		return
	}
	s.writeUndecidable(w, r, action, err)
}

func (s *Server) writeUndecidable(w http.ResponseWriter, r *http.Request, action string, err error) {
	s.log.Record(r.Context(), diag.Event{
		Component: "httpapi", Action: action, Outcome: diag.Undecided,
		Category: "INTERNAL", Reason: err.Error(),
	})
	s.fail(w, r, http.StatusServiceUnavailable, "UNDECIDABLE",
		"the service could not determine a result; safe to retry",
		map[string]any{"action": action})
}

func (s *Server) decode(w http.ResponseWriter, r *http.Request, v any) bool {
	body := http.MaxBytesReader(w, r.Body, 1<<20)
	dec := json.NewDecoder(body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		s.fail(w, r, http.StatusBadRequest, "MALFORMED_JSON",
			"request body must be valid JSON: "+err.Error(), nil)
		return false
	}
	if dec.More() {
		s.fail(w, r, http.StatusBadRequest, "MALFORMED_JSON",
			"request body must contain a single JSON object", nil)
		return false
	}
	return true
}

type errBody struct {
	Error     string         `json:"error"`
	Category  string         `json:"category"`
	Detail    string         `json:"detail,omitempty"`
	RequestID string         `json:"request_id"`
	KeyState  map[string]any `json:"key_state,omitempty"`
}

func (s *Server) fail(w http.ResponseWriter, r *http.Request, status int, category, detail string, state map[string]any) {
	outcome := diag.Rejected
	if status >= 500 {
		outcome = diag.Undecided
	}
	s.log.Record(r.Context(), diag.Event{
		Component: "httpapi", Action: r.Method + " " + r.URL.Path,
		Outcome: outcome, Category: category, KeyState: state, Reason: detail,
	})
	writeJSON(w, status, errBody{
		Error:     http.StatusText(status),
		Category:  category,
		Detail:    detail,
		RequestID: diag.RequestID(r.Context()),
		KeyState:  state,
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

func (s *Server) requestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-Id")
		if id == "" {
			id = diag.NewRequestID()
		}
		w.Header().Set("X-Request-Id", id)
		ctx := diag.WithRequestID(r.Context(), id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func (s *Server) recoverPanic(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				s.log.Record(r.Context(), diag.Event{
					Component: "httpapi", Action: "panic",
					Outcome: diag.Undecided, Category: "PANIC",
					Reason: "handler panicked",
					KeyState: map[string]any{"panic": strconv.Quote(toString(rec))},
				})
				s.fail(w, r, http.StatusInternalServerError, "PANIC",
					"internal error", nil)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func toString(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case error:
		return t.Error()
	default:
		return "unknown"
	}
}

var _ = io.EOF
