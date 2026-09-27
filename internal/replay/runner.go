// Package replay is the orchestration boundary: it validates a Config,
// builds the topology index, runs the engine with a generated run id and
// logger, and persists the outcome. Input handling, engine computation and
// storage stay independently testable behind this seam.
package replay

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"sync"
	"time"

	"pathvector/internal/config"
	"pathvector/internal/engine"
	"pathvector/internal/ierr"
	"pathvector/internal/store"
)

// RunStore is the persistence seam (satisfied by *store.Store; tests can
// fake it).
type RunStore interface {
	SaveRun(ctx context.Context, in store.SaveInput) error
}

// lineLogger fans structured "key=value" lines to multiple writers while
// serializing concurrent runs.
type lineLogger struct {
	mu *sync.Mutex
	ws []io.Writer
	id string
}

func (l *lineLogger) Logf(format string, args ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	line := time.Now().UTC().Format("15:04:05.000000") + " " + fmt.Sprintf(format, args...) + "\n"
	for _, w := range l.ws {
		_, _ = io.WriteString(w, line)
	}
}

// Runner executes and archives replays.
type Runner struct {
	store RunStore
	logMu sync.Mutex
	logWs []io.Writer
	// NewID overrides run id generation (tests).
	NewID func() string
}

// NewRunner constructs a Runner. Extra log writers receive every decision
// log line (stdout in the server; test files in tests).
func NewRunner(st RunStore, logWriters ...io.Writer) *Runner {
	return &Runner{store: st, logWs: logWriters, NewID: GenerateRunID}
}

// GenerateRunID returns a time-ordered, collision-resistant run identifier
// used to correlate logs, traces and stored records.
func GenerateRunID() string {
	var b [6]byte
	_, _ = rand.Read(b[:])
	return "run-" + time.Now().UTC().Format("20060102T150405.000000Z") + "-" + hex.EncodeToString(b[:])
}

// Result is returned to API callers.
type Result struct {
	RunID  string         `json:"run_id"`
	Report *engine.Report `json:"report"`
	// ErrorKind/Detail are populated on hard failure alongside the report.
	ErrorKind   string `json:"error_kind,omitempty"`
	ErrorDetail string `json:"error_detail,omitempty"`
}

// Execute validates and runs one inline config payload.
func (r *Runner) Execute(ctx context.Context, raw []byte) (*Result, error) {
	cfg, err := config.Parse(raw)
	if err != nil {
		return nil, err
	}
	return r.run(ctx, cfg, raw)
}

// ExecuteConfig runs an already-parsed fixture config (raw is the exact
// submitted bytes stored for replay, or nil to re-marshal the config).
func (r *Runner) ExecuteConfig(ctx context.Context, cfg *config.Config, raw []byte) (*Result, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	if raw == nil {
		raw, _ = json.Marshal(cfg)
	}
	return r.run(ctx, cfg, raw)
}

func (r *Runner) run(ctx context.Context, cfg *config.Config, raw []byte) (*Result, error) {
	runID := r.NewID()
	logger := &lineLogger{mu: &r.logMu, ws: r.logWs, id: runID}
	logger.Logf("run=%s start seeds=%d budget=%d queue_cap=%d nodes=%d",
		runID, len(cfg.Events), cfg.Budget, cfg.QueueCap, len(cfg.Topology.Nodes))

	idx, err := cfg.Topology.Build()
	if err != nil {
		return nil, err // already classified by config
	}
	report, runErr := engine.Run(runID, idx, cfg.Policies, cfg.Events, engine.Options{
		Budget:   cfg.Budget,
		QueueCap: cfg.QueueCap,
		Logger:   logger,
	})

	res := &Result{RunID: runID, Report: report}
	in := store.SaveInput{
		RunID:      runID,
		Status:     string(report.Status),
		Reason:     report.Reason,
		Steps:      report.Steps,
		Budget:     report.Budget,
		ConfigJSON: raw,
		Report:     report,
	}
	if runErr != nil {
		in.ErrorKind = string(ierr.Of(runErr))
		in.ErrorDetail = runErr.Error()
		res.ErrorKind = in.ErrorKind
		res.ErrorDetail = in.ErrorDetail
		logger.Logf("run=%s FAILED kind=%s detail=%q", runID, in.ErrorKind, in.ErrorDetail)
	} else {
		logger.Logf("run=%s done status=%s reason=%s steps=%d",
			runID, report.Status, report.Reason, report.Steps)
	}
	if r.store != nil {
		if err := r.store.SaveRun(ctx, in); err != nil {
			return res, ierr.Wrap(ierr.Of(err), "replay.run", "persist run "+runID, err)
		}
	}
	if runErr != nil {
		return res, runErr
	}
	return res, nil
}
