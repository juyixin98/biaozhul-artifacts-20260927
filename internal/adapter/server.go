// Package adapter exposes the controller over HTTP using only the standard
// library. Every response is correlated by a request id; errors return a
// structured envelope. The HTTP layer owns no policy.
package adapter

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/controller"
	"replicactl/internal/model"
)

// Server wires the engine and the persistence/actuator port to HTTP.
type Server struct {
	engine *controller.Engine
	store  Store
	cfg    func() (config.Config, error)
	log    *slog.Logger

	now func() time.Time
}

// Store is the subset of persistence the handlers need beyond the
// controller's Port (ingestion and read endpoints).
type Store interface {
	InsertSample(context.Context, model.Sample) error
	InsertDemand(context.Context, model.Demand) error
	Fleet(context.Context) (model.Fleet, error)
	Decisions(context.Context, int) ([]model.Decision, error)
	DecisionByRequestID(context.Context, string) (model.Decision, error)
	LoadConfig(context.Context) (config.Config, int64, error)
	SaveConfig(context.Context, config.Config, string) (int64, error)
	PruneObservations(context.Context, time.Time) error
}

// NewServer builds the HTTP server.
func NewServer(engine *controller.Engine, store Store, log *slog.Logger, now func() time.Time) *Server {
	if now == nil {
		now = func() time.Time { return time.Now().UTC() }
	}
	return &Server{engine: engine, store: store, log: log, now: now}
}

// Handler returns the routed http.Handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /config", s.handleGetConfig)
	mux.HandleFunc("PUT /config", s.handlePutConfig)
	mux.HandleFunc("POST /v1/instances/{id}/samples", s.handleSample)
	mux.HandleFunc("POST /v1/demand", s.handleDemand)
	mux.HandleFunc("POST /v1/reconcile", s.handleReconcile)
	mux.HandleFunc("GET /v1/fleet", s.handleFleet)
	mux.HandleFunc("GET /v1/decisions", s.handleDecisions)
	mux.HandleFunc("GET /v1/decisions/{requestID}", s.handleDecision)
	return s.withRequestID(mux)
}

// withRequestID assigns/propagates an id used in logs and the decision record.
func (s *Server) withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rid := r.Header.Get("X-Request-ID")
		if rid == "" {
			b := make([]byte, 8)
			_, _ = rand.Read(b)
			rid = "req-" + hex.EncodeToString(b)
		}
		ctx := context.WithValue(r.Context(), ridKey{}, rid)
		w.Header().Set("X-Request-ID", rid)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

type ridKey struct{}

func requestID(ctx context.Context) string {
	if v, ok := ctx.Value(ridKey{}).(string); ok {
		return v
	}
	return "req-unknown"
}

// --- envelope helpers ------------------------------------------------------

type errBody struct {
	Error   string `json:"error"`
	Code    string `json:"code"`
	Request string `json:"request_id"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, r *http.Request, status int, code, msg string) {
	writeJSON(w, status, errBody{Error: msg, Code: code, Request: requestID(r.Context())})
}

// handleHealth is a liveness probe.
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":     "ok",
		"request_id": requestID(r.Context()),
	})
}
