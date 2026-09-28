// Package reconciler ties observation, planning, durable journaling and the
// provider adapter into one coordination loop. It is responsible for the two
// safety properties the project is built around:
//
//  1. A plan is bound to an observation fingerprint; if the world drifted
//     before application, the apply is refused.
//  2. After an interruption, resume reconciles strictly against the real
//     observed outcome, so a committed-but-unacknowledged create is never
//     issued twice and a delete is never repeated against a reassigned ID.
package reconciler

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"

	"infraplanner/internal/journal"
	"infraplanner/internal/model"
	"infraplanner/internal/planner"
	"infraplanner/internal/provider"
)

// PlanRequest is the HTTP-facing planning request.
type PlanRequest struct {
	Resources     []model.Desired `json:"resources"`
	ReleaseGuards []model.Key     `json:"release_guards,omitempty"`
}

// GuardBlock names a protected resource that stopped the plan.
type GuardBlock struct {
	Key    model.Key `json:"key"`
	Reason string    `json:"reason"`
}

// PlanResponse is returned by Plan.
type PlanResponse struct {
	RunID       string              `json:"run_id"`
	Fingerprint string              `json:"fingerprint"`
	Observed    int                 `json:"observed_count"`
	Operations  []planner.Operation `json:"operations"`
	Guarded     []model.Key         `json:"guarded,omitempty"`
	// PlanReady is false when guard blocks remain; the run is persisted but
	// cannot be applied until the guards are released in a new plan request.
	PlanReady bool `json:"plan_ready"`
}

// ApplyResponse is the terminal result of Apply/Resume.
type ApplyResponse struct {
	RunID     string             `json:"run_id"`
	State     model.RunState     `json:"state"`
	Completed int                `json:"completed_ops"`
	Total     int                `json:"total_ops"`
	Results   []OpResult         `json:"results"`
	Error     *TypedError        `json:"error,omitempty"`
	Evidence  []journal.Evidence `json:"-"`
	Extras    map[string]any     `json:"-"`
}

// TypedError mirrors model.Error for JSON.
type TypedError struct {
	Category string `json:"category"`
	Code     string `json:"code"`
	Message  string `json:"message"`
}

// OpResult is the outcome of one operation, suitable for assertions.
type OpResult struct {
	Seq        int            `json:"seq"`
	Type       planner.OpType `json:"type"`
	Key        model.Key      `json:"key"`
	State      model.OpState  `json:"state"`
	PhysicalID string         `json:"physical_id,omitempty"`
	Attempts   int            `json:"attempts"`
	Reason     string         `json:"reason,omitempty"`
	Error      *TypedError    `json:"error,omitempty"`
}

// Service is the coordination loop.
type Service struct {
	store *journal.Store
	prov  provider.Provider
	lg    Logger
	evDir string
	// MaxAttempts bounds retries of transient compute failures per op.
	MaxAttempts int
	// NewLogger builds a per-run logger; nil disables file logging.
	NewLogger func(runID string) (Logger, error)
}

// Logger is the logging surface the reconciler needs.
type Logger interface {
	Info(stage, msg string, fields map[string]any)
	Warn(stage, msg string, fields map[string]any)
	Error(stage, msg string, err error, fields map[string]any)
	Close() error
}

// New constructs a Service.
func New(store *journal.Store, prov provider.Provider, maxAttempts int) *Service {
	if maxAttempts <= 0 {
		maxAttempts = 3
	}
	return &Service{store: store, prov: prov, MaxAttempts: maxAttempts}
}

// SetLogger installs a logger factory.
func (s *Service) SetLogger(f func(runID string) (Logger, error)) { s.NewLogger = f }

// newRunID generates an externally opaque run id.
func newRunID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return "run-" + hex.EncodeToString(b[:])
}

// planDoc is the persisted plan envelope. The full baseline observation is
// embedded: the drift gate must compare against what was really observed at
// plan time, not a reconstruction from the desired spec.
type planDoc struct {
	Operations  []planner.Operation `json:"operations"`
	Fingerprint string              `json:"fingerprint"`
	Baseline    model.Observation   `json:"baseline"`
}

func encodePlan(p *planner.Plan) []byte {
	b, _ := json.Marshal(planDoc{
		Operations: p.Operations, Fingerprint: p.Fingerprint, Baseline: p.Baseline,
	})
	return b
}

func decodePlan(b []byte) ([]planner.Operation, string, model.Observation, error) {
	var d planDoc
	if err := json.Unmarshal(b, &d); err != nil {
		return nil, "", model.Observation{}, err
	}
	return d.Operations, d.Fingerprint, d.Baseline, nil
}
