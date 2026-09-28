// Package replay orchestrates one simulation end to end:
//
//	payload -> config.Parse -> engine.Run -> store persistence -> result
//
// It owns the mapping between failure categories and HTTP semantics and
// keeps the raw scenario bytes with every run so any reported run can be
// replayed byte-for-byte.
package replay

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"pvsim/config"
	"pvsim/engine"
	"pvsim/model"
	"pvsim/store"
)

// MaxPayloadBytes bounds a single submitted scenario (1 MiB).
const MaxPayloadBytes = 1 << 20

// Request is a replay submission.
type Request struct {
	// RunID is optional; when empty a time-ordered id is generated. When
	// supplied and already taken, Submit returns STATE_CONFLICT.
	RunID string `json:"run_id,omitempty"`
	// Scenario is the full scenario JSON.
	Scenario json.RawMessage `json:"scenario"`
}

// Summary is the compact result returned to callers and listed later.
type Summary struct {
	RunID             string                       `json:"run_id"`
	Name              string                       `json:"name"`
	Converged         bool                         `json:"converged"`
	NonConvergentCode string                       `json:"non_convergent_code,omitempty"`
	Steps             int                          `json:"steps"`
	Versions          int                          `json:"versions"`
	Best              map[string][]engine.BestView `json:"best"`
	Cycle             *engine.CycleEvidence        `json:"cycle,omitempty"`
}

// Service performs replays against a Store.
type Service struct {
	st *store.Store
}

// NewService constructs a replay service.
func NewService(st *store.Store) *Service { return &Service{st: st} }

// NewRunID returns a lexicographically time-ordered, collision-resistant
// run identifier: run-YYYYMMDDTHHMMSS-nnnnnnnnnnnn (UTC).
func NewRunID() string {
	var b [6]byte
	_, _ = rand.Read(b[:])
	return "run-" + time.Now().UTC().Format("20060102T150405") + "-" + hex.EncodeToString(b[:])
}

// Submit parses, executes and persists one replay.
//
// Mapping contract:
//
//	INPUT errors (parse/validate/payload) -> not executed, no row written;
//	RESOURCE_EXHAUSTED MAX_STEPS_CAP      -> not executed, no row written;
//	STATE_CONFLICT (run id taken)         -> not executed, existing row kept;
//	converged / not_converged             -> row written, result returned;
//	storage failures                      -> surfaced as typed errors.
func (s *Service) Submit(ctx context.Context, raw []byte, suppliedID string) (*Summary, *engine.Result, error) {
	if len(raw) > MaxPayloadBytes {
		return nil, nil, model.NewError(model.KindResourceExhausted, "PAYLOAD_TOO_LARGE",
			"scenario payload %d bytes exceeds %d byte limit", len(raw), MaxPayloadBytes)
	}
	var req Request
	if err := json.Unmarshal(raw, &req); err != nil {
		return nil, nil, model.NewError(model.KindInput, "PAYLOAD_SYNTAX",
			"request is not valid JSON: %v", err)
	}
	if len(req.Scenario) == 0 {
		return nil, nil, model.NewError(model.KindInput, "MISSING_SCENARIO",
			"request requires a \"scenario\" object")
	}

	sc, err := config.Parse(req.Scenario)
	if err != nil {
		return nil, nil, err // already a typed model.Error
	}

	runID := suppliedID
	if runID == "" {
		runID = req.RunID
	}
	if runID == "" {
		runID = NewRunID()
	}
	if !validRunID(runID) {
		return nil, nil, model.NewError(model.KindInput, "INVALID_RUN_ID",
			"run_id must match [A-Za-z0-9._-]{1,128}")
	}

	col := engine.NewCollector()
	res, err := engine.Run(sc, engine.Options{MaxSteps: sc.MaxSteps}, col)
	if err != nil {
		// Engine errors are typed; persist an error row so the failed
		// attempt remains observable, except for the not-executed classes.
		var me *model.Error
		if errors.As(err, &me) {
			if me.Kind == model.KindInput {
				return nil, nil, err
			}
		}
		_ = s.persistError(ctx, runID, sc.Name, string(req.Scenario), col, err)
		return nil, nil, err
	}

	resultJSON, _ := json.Marshal(res)
	var cycleJSON string
	if res.Cycle != nil {
		b, _ := json.Marshal(res.Cycle)
		cycleJSON = string(b)
	}
	status := store.StatusOK
	if !res.Converged {
		status = store.StatusNotConverged
	}
	rec := store.RunRecord{
		RunID: runID, Name: sc.Name, Status: status,
		Converged: res.Converged, NonConvergentCode: res.NonConvergentCode,
		Steps: res.Steps, Versions: res.Versions,
		CycleJSON: cycleJSON, ScenarioJSON: string(req.Scenario),
		ResultJSON: string(resultJSON),
	}
	saveErr := s.st.SaveRun(ctx, store.SaveParams{Record: rec, Collector: col})
	if saveErr != nil {
		if errors.Is(saveErr, store.ErrExists) {
			return nil, nil, model.NewError(model.KindStateConflict, "RUN_ID_EXISTS",
				"run %q already exists; choose a different run_id or omit it", runID)
		}
		var me *model.Error
		if errors.As(saveErr, &me) {
			return nil, nil, saveErr
		}
		return nil, nil, model.NewError(model.KindComputeFailed, "PERSISTENCE",
			"persist run: %v", saveErr)
	}
	return summary(runID, res), res, nil
}

func (s *Service) persistError(ctx context.Context, runID, name, scenarioJSON string,
	col *engine.Collector, runErr error) error {
	var me *model.Error
	if !errors.As(runErr, &me) {
		me = model.NewError(model.KindComputeFailed, "UNKNOWN", "%v", runErr)
	}
	rec := store.RunRecord{
		RunID: runID, Name: name, Status: store.StatusError,
		ScenarioJSON: scenarioJSON,
		ErrorKind:    string(me.Kind), ErrorCode: me.Code, ErrorMessage: me.Message,
	}
	if err := s.st.SaveRun(ctx, store.SaveParams{Record: rec, Collector: col}); err != nil {
		if errors.Is(err, store.ErrExists) {
			return model.NewError(model.KindStateConflict, "RUN_ID_EXISTS",
				"run %q already exists", runID)
		}
		return err
	}
	return nil
}

// Get returns the stored summary and full result of a run.
func (s *Service) Get(ctx context.Context, runID string) (*Summary, *engine.Result, error) {
	rec, err := s.st.GetRun(ctx, runID)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return nil, nil, model.NewError(model.KindNotFound, "RUN_NOT_FOUND",
				"no run with id %q", runID)
		}
		return nil, nil, model.NewError(model.KindComputeFailed, "PERSISTENCE", "%v", err)
	}
	if rec.Status == store.StatusError {
		return &Summary{RunID: rec.RunID, Name: rec.Name}, nil, model.NewError(
			model.Kind(rec.ErrorKind), rec.ErrorCode, "%s", rec.ErrorMessage)
	}
	var res engine.Result
	if err := json.Unmarshal([]byte(rec.ResultJSON), &res); err != nil {
		return nil, nil, model.NewError(model.KindComputeFailed, "CORRUPT_RECORD",
			"stored result JSON invalid: %v", err)
	}
	return summary(rec.RunID, &res), &res, nil
}

// Replay re-runs a stored scenario under a new run id and returns the new
// summary. The stored scenario bytes are parsed again so config changes
// in code between runs surface as INPUT errors.
func (s *Service) Replay(ctx context.Context, runID string) (*Summary, *engine.Result, error) {
	raw, err := s.st.RunScenarioJSON(ctx, runID)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return nil, nil, model.NewError(model.KindNotFound, "RUN_NOT_FOUND",
				"no run with id %q", runID)
		}
		return nil, nil, model.NewError(model.KindComputeFailed, "PERSISTENCE", "%v", err)
	}
	// Wrap the stored scenario object in a fresh request envelope so a new
	// run id is generated and the exact scenario bytes are re-validated.
	envelope, err := json.Marshal(Request{Scenario: json.RawMessage(raw)})
	if err != nil {
		return nil, nil, model.NewError(model.KindComputeFailed, "REPLAY_ENVELOPE",
			"rebuild replay request: %v", err)
	}
	return s.Submit(ctx, envelope, "")
}

func summary(runID string, res *engine.Result) *Summary {
	return &Summary{
		RunID: runID, Converged: res.Converged,
		NonConvergentCode: res.NonConvergentCode,
		Steps:             res.Steps, Versions: res.Versions,
		Best: res.Best, Cycle: res.Cycle,
	}
}

func validRunID(id string) bool {
	if len(id) == 0 || len(id) > 128 {
		return false
	}
	for _, r := range id {
		switch {
		case r >= 'a' && r <= 'z',
			r >= 'A' && r <= 'Z',
			r >= '0' && r <= '9',
			r == '-' || r == '_' || r == '.':
		default:
			return false
		}
	}
	return true
}

// DescribeError renders a typed error for logs.
func DescribeError(err error) string {
	var me *model.Error
	if errors.As(err, &me) {
		return fmt.Sprintf("%s/%s: %s", me.Kind, me.Code, me.Message)
	}
	return err.Error()
}
