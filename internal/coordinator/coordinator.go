// Package coordinator wires the pipeline to storage: it owns the synchronous
// admission path used by the HTTP adapter and the asynchronous reconciliation
// loop that drains pending requests and reclaims stale leases after a crash.
//
// Re-entrancy: admitting the same UID twice is idempotent. A terminal request
// row returns its stored verdict (including the same final-object digest); an
// in-flight UID is a state conflict; a stale processing lease is reclaimed by
// ReconcileOnce. The quota ledger keys holds by UID, so a retried reserve
// after a crash never double counts.
package coordinator

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"admission/internal/model"
	"admission/internal/pipeline"
	"admission/internal/plugins"
	"admission/internal/runlog"
	"admission/internal/storage"
)

// Clock abstracts time for deterministic tests.
type Clock func() time.Time

// Coordinator holds dependencies; construct with New.
type Coordinator struct {
	store    *storage.Store
	pipe     *pipeline.Pipeline
	log      *runlog.Logger
	clock    Clock
	lease    time.Duration
	runSeq   int64
	mu       sync.Mutex
	inflight map[string]struct{}
}

// Option configures a Coordinator.
type Option func(*Coordinator)

// WithLease sets the processing-lease duration.
func WithLease(d time.Duration) Option {
	return func(c *Coordinator) { c.lease = d }
}

// New builds a Coordinator.
func New(store *storage.Store, pipe *pipeline.Pipeline, log *runlog.Logger, opts ...Option) *Coordinator {
	c := &Coordinator{
		store:    store,
		pipe:     pipe,
		log:      log,
		clock:    time.Now,
		lease:    30 * time.Second,
		inflight: map[string]struct{}{},
	}
	for _, o := range opts {
		o(c)
	}
	return c
}

// AdmitResult is the coordinator-level outcome. When Stored is true the
// verdict was replayed from an existing terminal request row (idempotent
// re-entry).
type AdmitResult struct {
	Response model.Response
	Stored   bool
}

// Admit runs admission for a validated request and persists the outcome.
func (c *Coordinator) Admit(ctx context.Context, req model.Request) (AdmitResult, error) {
	if err := req.Validate(); err != nil {
		return AdmitResult{}, &InputError{err.Error()}
	}
	payload, err := json.Marshal(req)
	if err != nil {
		return AdmitResult{}, err
	}
	if !c.tryAcquire(req.UID) {
		return AdmitResult{}, &StateError{model.ReasonStateConflict, "request is already being processed in this process"}
	}
	defer c.release(req.UID)

	now := c.nowMs()
	existed, err := c.store.InsertRequest(ctx, string(payload), req.UID, req.Operation, now)
	if err != nil {
		return AdmitResult{}, err
	}
	if existed {
		row, lerr := c.store.GetRequest(ctx, req.UID)
		if lerr != nil {
			return AdmitResult{}, lerr
		}
		switch row.Status {
		case storage.StatusAllowed, storage.StatusDenied:
			return c.replayStored(ctx, req.UID)
		case storage.StatusFailed, storage.StatusPending:
			// retryable: re-claim under this lease and reprocess below
		case storage.StatusProcessing:
			if row.LeaseUntil > now {
				return AdmitResult{}, &StateError{model.ReasonStateConflict,
					fmt.Sprintf("request %s is being processed", req.UID)}
			}
			// stale lease: reclaim below
		}
	}
	if err := c.store.ClaimUID(ctx, req.UID, now, now+c.lease.Milliseconds()); err != nil {
		return AdmitResult{}, err
	}
	return c.process(ctx, req.UID, string(payload))
}

func (c *Coordinator) replayStored(ctx context.Context, uid string) (AdmitResult, error) {
	audits, err := c.store.LoadAudits(ctx, uid)
	if err != nil {
		return AdmitResult{}, err
	}
	if len(audits) == 0 {
		return AdmitResult{}, fmt.Errorf("terminal request %s has no audit record", uid)
	}
	return AdmitResult{Response: audits[0].Resp, Stored: true}, nil
}

// process claims the request row inside this process, runs the pipeline and
// commits the outcome. It is shared by the synchronous path and the
// reconciler.
func (c *Coordinator) process(ctx context.Context, uid, payload string) (AdmitResult, error) {
	var req model.Request
	if err := json.Unmarshal([]byte(payload), &req); err != nil {
		_ = c.store.MarkFailed(ctx, uid, "corrupt payload: "+err.Error(), c.nowMs())
		return AdmitResult{}, &InputError{"stored request payload is corrupt: " + err.Error()}
	}

	runID := c.nextRunID()
	start := c.clock()
	result := c.pipe.Run(ctx, req)
	resp := model.Response{
		UID:        uid,
		Steps:      result.Steps,
		Patches:    result.Patches,
		DurationMS: c.clock().Sub(start).Milliseconds(),
	}
	if result.Allowed {
		resp.Decision = model.DecisionAllowed
		resp.FinalObject = result.Final
		resp.FinalSummary = result.Summary
	} else {
		resp.Decision = model.DecisionDenied
		resp.Reason = result.Reason
		resp.Message = result.Message
		// Bind the last observed object summary even on denial, when the
		// pipeline produced one, so the audit shows *what* was rejected.
		if len(result.Final) > 0 {
			if sum, err := model.Summarize(result.Final); err == nil {
				resp.FinalSummary = &sum
				resp.FinalObject = result.Final
			}
		}
	}

	commitErr := c.store.CommitOutcome(ctx, runID, req, resp, c.nowMs())
	if commitErr != nil {
		if errors.Is(commitErr, plugins.ErrQuotaExhausted) {
			// The authoritative commit gate (against real committed usage +
			// other in-flight holds) rejected a request the early validator
			// allowed — possible only under concurrent contention. Persist it
			// as a normal resource_exhausted denial rather than a 5xx.
			denied := model.Response{
				UID:          uid,
				Decision:     model.DecisionDenied,
				Reason:       model.ReasonResourceExhausted,
				Message:      commitErr.Error(),
				Steps:        resp.Steps,
				FinalObject:  resp.FinalObject,
				FinalSummary: resp.FinalSummary,
				DurationMS:   resp.DurationMS,
			}
			if derr := c.store.CommitOutcome(ctx, runID, req, denied, c.nowMs()); derr != nil {
				return AdmitResult{}, fmt.Errorf("commit quota denial: %w (original: %v)", derr, commitErr)
			}
			c.appendLog(runID, req, denied)
			return AdmitResult{Response: denied}, nil
		}
		if errors.Is(commitErr, storage.ErrConflict) {
			return AdmitResult{}, &StateError{model.ReasonStateConflict,
				"resource already exists: " + commitErr.Error()}
		}
		if errors.Is(commitErr, storage.ErrNotFound) {
			return AdmitResult{}, &StateError{model.ReasonStateConflict,
				"update/delete target missing: " + commitErr.Error()}
		}
		return AdmitResult{}, fmt.Errorf("commit outcome: %w", commitErr)
	}

	c.appendLog(runID, req, resp)
	return AdmitResult{Response: resp}, nil
}

func (c *Coordinator) appendLog(runID string, req model.Request, resp model.Response) {
	_ = c.log.Append(runlog.Line{
		RunID:        runID,
		Time:         c.clock(),
		Request:      req,
		Decision:     resp.Decision,
		Reason:       resp.Reason,
		Category:     resp.Reason.Category(),
		Message:      resp.Message,
		Steps:        resp.Steps,
		FinalSummary: resp.FinalSummary,
	})
}

// ReconcileOnce claims one pending or stale-processing request and admits it.
// It returns false when nothing was claimable.
func (c *Coordinator) ReconcileOnce(ctx context.Context) (bool, error) {
	now := c.nowMs()
	uid, payload, ok, err := c.store.Claim(ctx, now, now+c.lease.Milliseconds())
	if err != nil || !ok {
		return false, err
	}
	if !c.tryAcquire(uid) {
		// Claimed by this process synchronously but still within lease — leave
		// it for lease expiry rather than double-processing.
		return false, nil
	}
	defer c.release(uid)
	_, err = c.process(ctx, uid, payload)
	return true, err
}

// RunReconcile blocks, draining work each interval until ctx is canceled.
func (c *Coordinator) RunReconcile(ctx context.Context, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			for {
				did, err := c.ReconcileOnce(ctx)
				if err != nil || !did {
					break
				}
			}
		}
	}
}

func (c *Coordinator) tryAcquire(uid string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, busy := c.inflight[uid]; busy {
		return false
	}
	c.inflight[uid] = struct{}{}
	return true
}

func (c *Coordinator) release(uid string) {
	c.mu.Lock()
	delete(c.inflight, uid)
	c.mu.Unlock()
}

func (c *Coordinator) nowMs() int64 { return c.clock().UnixMilli() }

// nextRunID produces a human-greppable, process-unique run identifier:
// run-20260928T150405Z-000007.
func (c *Coordinator) nextRunID() string {
	c.mu.Lock()
	c.runSeq++
	n := c.runSeq
	c.mu.Unlock()
	return fmt.Sprintf("run-%s-%06d", c.clock().UTC().Format("20060102T150405Z"), n)
}

// InputError marks request-level 400s.
type InputError struct{ Msg string }

func (e *InputError) Error() string { return e.Msg }

// StateError marks state conflicts with an explicit reason.
type StateError struct {
	Reason model.Reason
	Msg    string
}

func (e *StateError) Error() string { return e.Msg }
