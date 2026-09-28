// Package service wires the pipeline to persistence and the quota adapter:
// duplicate-call handling, quota reservation after a successful chain, audit
// events bound to the final object summary, and retry classification for the
// reconciler.
package service

import (
	"context"
	"fmt"
	"strconv"
	"sync"
	"time"

	"admission/internal/admission"
	"admission/internal/quota"
	"admission/internal/storage"
	"admission/internal/types"
)

// IDGenerator produces replay-traceable run IDs.
type IDGenerator func() string

// Service is safe for concurrent use.
type Service struct {
	pipe   *admission.Pipeline
	store  storage.Store
	ledger quota.Adapter
	newID  IDGenerator
	now    func() time.Time
}

// Deps bundles constructor dependencies.
type Deps struct {
	Pipeline *admission.Pipeline
	Store    storage.Store
	Ledger   quota.Adapter // may be nil when the quota validator is not configured
	NewID    IDGenerator
	Now      func() time.Time
}

// New constructs a Service.
func New(d Deps) (*Service, error) {
	if d.Pipeline == nil || d.Store == nil {
		return nil, fmt.Errorf("service: pipeline and store are required")
	}
	if d.NewID == nil {
		d.NewID = defaultID
	}
	if d.Now == nil {
		d.Now = time.Now
	}
	return &Service{
		pipe: d.Pipeline, store: d.Store, ledger: d.Ledger,
		newID: d.NewID, now: d.Now,
	}, nil
}

// Admit runs one admission attempt with an explicit run ID (empty means one is
// generated). Attempt is the 1-based reconciliation attempt number.
func (s *Service) Admit(ctx context.Context, runID string, req types.Review, attempt int) types.Response {
	if runID == "" {
		runID = s.newID()
	}
	if attempt < 1 {
		attempt = 1
	}
	started := s.now()

	// Duplicate call: a stored verdict is returned verbatim and the chain is
	// never re-executed.
	if prior, ok, err := s.store.LookupUID(ctx, req.UID); err == nil && ok {
		prior.Replayed = true
		prior.RunID = runID
		s.record(ctx, auditFromReplay(runID, attempt, prior, started, s.now()))
		return prior
	}

	res := s.pipe.Run(ctx, runID, req)
	finished := s.now()
	resp := types.Response{
		UID: req.UID, RunID: runID, Operation: req.Operation, DryRun: req.DryRun,
		Object: req.Object, Steps: res.Steps, Attempt: attempt, FinishedAt: finished,
	}

	if res.Err != nil {
		resp.Allowed = false
		resp.FailureCategory = res.Err.Category
		resp.FailurePhase = res.Err.Phase
		resp.FailedPlugin = res.Err.Plugin
		resp.Message = res.Err.Message
		resp.Final = types.Summarize(req.Object) // failed run: object unchanged
		s.terminal(ctx, req, resp, started, finished)
		return resp
	}

	resp.Object = res.Object
	resp.Final = types.Summarize(res.Object)
	if !res.Allowed {
		// Clean policy denial: category stays empty; this is terminal.
		resp.DenyReason = res.Deny
		resp.Message = res.Deny
		s.terminal(ctx, req, resp, started, finished)
		return resp
	}

	// Chain allowed: reserve external capacity (the only side effect), unless
	// this is a dry run. Reservation is idempotent under UID.
	if s.ledger != nil && !req.DryRun && req.Operation == "CREATE" {
		cpu, mem, err := quota.RequestFromObject(res.Object)
		if err != nil {
			resp.Allowed = false
			resp.FailureCategory = types.CatInvalidInput
			resp.FailurePhase = types.PhaseValidating
			resp.Message = "post-chain quota parse: " + err.Error()
			resp.Final = types.Summarize(res.Object)
			s.terminal(ctx, req, resp, started, finished)
			return resp
		}
		if err := s.ledger.Reserve(req.UID, cpu, mem); err != nil {
			cat := types.CatComputeFailure
			msg := err.Error()
			if ex, ok := err.(*quota.ExhaustedError); ok {
				cat = types.CatQuotaExhausted
				msg = fmt.Sprintf("reserve lost race on %s (used cpu=%dm mem=%d, capacity cpu=%dm mem=%d)",
					ex.Resource, ex.UsedCPU, ex.UsedMemory, ex.CapacityCPU, ex.CapacityMem)
			}
			resp.Allowed = false
			resp.FailureCategory = cat
			resp.FailurePhase = types.PhaseValidating
			resp.FailedPlugin = "quota-adapter"
			resp.Message = msg
			s.terminal(ctx, req, resp, started, finished)
			return resp
		}
	}

	resp.Allowed = true
	if !req.DryRun && req.Operation == "CREATE" {
		// Admission is what makes the object immutable afterwards. This is the
		// one sanctioned writer of the guarded flag, and it happens in the
		// service — never via a mutator patch.
		resp.Object.Spec.Immutable = true
		resp.Final = types.Summarize(resp.Object)
	}
	s.terminal(ctx, req, resp, started, finished)
	return resp
}

// terminal persists response + audit. Only terminal verdicts are stored as
// the UID's response: a retryable failure that the loop will attempt again
// must NOT shadow the eventual outcome. Audit rows are written for every
// attempt regardless (with Terminal=false while the request remains queued).
func (s *Service) terminal(ctx context.Context, req types.Review, resp types.Response, started, finished time.Time) {
	terminalVerdict := resp.Allowed || !resp.FailureCategory.Retryable() || resp.DryRun
	ev := buildAudit(resp.RunID, attemptFromResp(resp), req, resp, started, finished, terminalVerdict)
	if !resp.DryRun && terminalVerdict {
		if err := s.store.SaveResponse(ctx, resp); err != nil {
			resp.Allowed = false
			resp.FailureCategory = types.CatComputeFailure
			resp.Message = "persist verdict: " + err.Error()
		}
	}
	s.record(ctx, ev)
}

func (s *Service) record(ctx context.Context, ev types.AuditEvent) {
	_ = s.store.AppendAudit(ctx, ev)
}

func attemptFromResp(resp types.Response) int {
	if resp.Attempt < 1 {
		return 1
	}
	return resp.Attempt
}

func buildAudit(runID string, attempt int, req types.Review, resp types.Response,
	started, finished time.Time, terminal bool) types.AuditEvent {
	before := req.Object.Fingerprint()
	after := resp.Object.Fingerprint()
	return types.AuditEvent{
		RunID: runID, UID: resp.UID, Attempt: attempt, Terminal: terminal,
		Operation: req.Operation, Kind: req.Object.Kind, DryRun: resp.DryRun,
		Allowed: resp.Allowed, Replayed: resp.Replayed, DenyReason: resp.DenyReason,
		BeforeHash: before, AfterHash: after, Final: resp.Final, Steps: resp.Steps,
		Category: resp.FailureCategory, FailurePhase: resp.FailurePhase,
		FailedPlugin: resp.FailedPlugin, Message: resp.Message,
		StartedAt: started, FinishedAt: finished,
	}
}

func auditFromReplay(runID string, attempt int, resp types.Response, started, finished time.Time) types.AuditEvent {
	return types.AuditEvent{
		RunID: runID, UID: resp.UID, Attempt: attempt, Terminal: true,
		Operation: resp.Operation, Kind: resp.Object.Kind, DryRun: resp.DryRun,
		Allowed: resp.Allowed, Replayed: true, DenyReason: resp.DenyReason,
		BeforeHash: resp.Final.Fingerprint, AfterHash: resp.Final.Fingerprint,
		Final: resp.Final, Steps: resp.Steps, Category: resp.FailureCategory,
		FailurePhase: resp.FailurePhase, FailedPlugin: resp.FailedPlugin,
		Message:   "duplicate UID: stored verdict replayed, chain not re-run",
		StartedAt: started, FinishedAt: finished,
	}
}

// Retryable reports whether the response category may be retried by the
// reconciler.
func Retryable(resp types.Response) bool {
	return !resp.Allowed && resp.FailureCategory.Retryable() && !resp.DryRun
}

// defaultID generates a process-unique run identifier.
func defaultID() string {
	return "run-" + strconv.FormatInt(time.Now().UnixNano(), 36) + "-" + strconv.Itoa(globalSeq())
}

var idMu sync.Mutex
var idSeq int

func globalSeq() int {
	idMu.Lock()
	defer idMu.Unlock()
	idSeq++
	return idSeq
}
