// Package coord runs the reconciliation loop: it serializes applies per
// resource (one writer per resource id, many resources in parallel), invokes
// the pure merge engine, and commits the result together with ownership and
// history. A bounded pending queue makes back-pressure observable as a
// resource_exhausted error instead of silent memory growth.
package coord

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"time"

	"fieldapply/internal/diag"
	"fieldapply/internal/merge"
	"fieldapply/internal/model"
	"fieldapply/internal/store"
)

// ApplyRequest is an apply call from an adapter.
type ApplyRequest struct {
	ResourceID string
	Manager    string
	Config     json.RawMessage
	Force      bool
	BaseRev    int64 // optional: -1 skips the check
	Reason     string
}

// ApplyOutcome mirrors the persisted result for adapters.
type ApplyOutcome struct {
	RunID     string          `json:"run_id"`
	Revision  int64           `json:"revision"`
	Live      json.RawMessage `json:"live"`
	Changes   model.ChangeSet `json:"changes"`
	NewOwners []string        `json:"new_owners,omitempty"`
	Forced    bool            `json:"forced"`
}

// Coordinator owns the per-resource serialization loops.
type Coordinator struct {
	st     store.Store
	log    *diag.Logger
	queues map[string]chan *job
	mu     sync.Mutex
	wg     sync.WaitGroup
	stop   chan struct{}
	closed bool
}

// New constructs a coordinator.
func New(st store.Store, log *diag.Logger) *Coordinator {
	return &Coordinator{
		st:     st,
		log:    log,
		queues: map[string]chan *job{},
		stop:   make(chan struct{}),
	}
}

type job struct {
	ctx context.Context
	req ApplyRequest
	run string
	res chan<- result
}

type result struct {
	out *ApplyOutcome
	err error
}

// Close drains the loops. After Close no new applies are accepted.
func (c *Coordinator) Close() {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return
	}
	c.closed = true
	qs := c.queues
	c.queues = map[string]chan *job{}
	close(c.stop)
	c.mu.Unlock()
	for _, q := range qs {
		close(q)
	}
	c.wg.Wait()
}

// Apply enqueues a single apply. The queue bound (per resource) rejects
// overloads up front with resource_exhausted/queue_full.
func (c *Coordinator) Apply(ctx context.Context, req ApplyRequest) (*ApplyOutcome, error) {
	const perResourceQueue = 32

	if req.Manager == "" {
		return nil, &model.Error{Category: model.CatInvalidInput, Code: "manager_required",
			Message: "field manager must not be empty"}
	}
	if len(req.Config) == 0 {
		return nil, &model.Error{Category: model.CatInvalidInput, Code: "config_required",
			Message: "config must be a JSON object"}
	}
	cfg, err := model.DecodeValue(req.Config)
	if err != nil {
		return nil, &model.Error{Category: model.CatInvalidInput, Code: "config_invalid_json",
			Message: "config is not valid JSON: " + err.Error()}
	}
	if _, ok := cfg.(map[string]any); !ok {
		return nil, &model.Error{Category: model.CatInvalidInput, Code: "config_not_object",
			Message: "config must be a JSON object"}
	}

	runID := diag.NewRunID()
	c.log.Log(diag.Event{
		RunID: runID, Phase: "received", ResourceID: req.ResourceID,
		Manager: req.Manager, Force: req.Force, Reason: req.Reason,
		Summary: "apply enqueued",
	})

	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil, &model.Error{Category: model.CatResourceExhausted, Code: "coordinator_closed",
			Message: "coordinator is shutting down"}
	}
	q, ok := c.queues[req.ResourceID]
	if !ok {
		q = make(chan *job, perResourceQueue)
		c.queues[req.ResourceID] = q
		c.wg.Add(1)
		go c.loop(req.ResourceID, q)
	}
	c.mu.Unlock()

	rc := make(chan result, 1)
	j := &job{ctx: ctx, req: req, run: runID, res: rc}
	select {
	case q <- j:
	default:
		c.log.Log(diag.Event{RunID: runID, Phase: "error", ResourceID: req.ResourceID,
			Manager: req.Manager, Category: string(model.CatResourceExhausted),
			Code: "queue_full", Summary: "per-resource apply queue saturated"})
		return nil, &model.Error{Category: model.CatResourceExhausted, Code: "queue_full",
			Message: "too many pending applies for resource " + req.ResourceID + "; retry"}
	}

	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case r := <-rc:
		return r.out, r.err
	}
}

func (c *Coordinator) loop(id string, q <-chan *job) {
	defer c.wg.Done()
	for j := range q {
		out, err := c.execute(j.ctx, id, j.run, j.req)
		j.res <- result{out: out, err: err}
	}
}

// execute performs load -> pure merge -> commit. It runs while holding the
// resource's single-writer slot, so the snapshot and the revision check inside
// Commit cannot interleave with another apply to the same resource.
func (c *Coordinator) execute(ctx context.Context, id, runID string, req ApplyRequest) (out *ApplyOutcome, err error) {
	defer func() {
		if r := recover(); r != nil {
			c.log.Log(diag.Event{RunID: runID, Phase: "error", ResourceID: id,
				Manager: req.Manager, Category: string(model.CatComputationFailure),
				Code: "panic", Summary: fmtErr(r)})
			err = &model.Error{Category: model.CatComputationFailure, Code: "internal_panic",
				Message: fmtErr(r)}
		}
	}()

	snap, err := c.st.Snapshot(ctx, id)
	if err != nil {
		c.log.Log(diag.Event{RunID: runID, Phase: "error", ResourceID: id,
			Manager: req.Manager, Category: categoryOf(err), Code: codeOf(err),
			Summary: err.Error()})
		return nil, err
	}

	live, err := model.DecodeValue(snap.Live)
	if err != nil {
		return nil, &model.Error{Category: model.CatComputationFailure, Code: "live_decode",
			Message: err.Error()}
	}
	cfg, _ := model.DecodeValue(req.Config)

	var previous any
	if prevRaw, ok, err := c.st.AppliedOf(ctx, id, req.Manager); err != nil {
		return nil, err
	} else if ok {
		previous, err = model.DecodeValue(prevRaw)
		if err != nil {
			return nil, &model.Error{Category: model.CatComputationFailure, Code: "applied_decode",
				Message: err.Error()}
		}
	}

	base := req.BaseRev
	if base == 0 {
		base = -1
	}

	mr, err := merge.Apply(&merge.Inputs{
		ResourceID: id,
		Manager:    req.Manager,
		Live:       live,
		Owners:     snap.Owners,
		Previous:   previous,
		Config:     cfg,
		Force:      req.Force,
		Schema:     snap.Schema,
		BaseRev:    base,
		Rev:        snap.Revision,
	})
	if err != nil {
		se, _ := model.AsError(err)
		detail, _ := json.Marshal(se.Conflicts)
		c.log.Log(diag.Event{RunID: runID, Phase: "conflict", ResourceID: id,
			Manager: req.Manager, Revision: &snap.Revision, Force: req.Force,
			Category: string(se.Category), Code: se.Code, Summary: se.Message,
			Detail: detail})
		return nil, err
	}

	liveRaw, _ := json.Marshal(mr.Live)
	appliedRaw, _ := json.Marshal(mr.Applied)
	reason := req.Reason
	if reason == "" {
		reason = "apply"
	}
	nextRev, err := c.st.Commit(ctx, id, store.Commit{
		Manager:   req.Manager,
		Reason:    reason,
		RunID:     runID,
		BaseRev:   snap.Revision,
		Live:      liveRaw,
		Applied:   appliedRaw,
		Owners:    mr.Owners,
		Changes:   mr.Changes,
		NewOwners: mr.NewOwners,
		Forced:    req.Force,
		At:        time.Now().UTC(),
	})
	if err != nil {
		c.log.Log(diag.Event{RunID: runID, Phase: "error", ResourceID: id,
			Manager: req.Manager, Category: categoryOf(err), Code: codeOf(err),
			Summary: err.Error()})
		return nil, err
	}

	c.log.Log(diag.Event{RunID: runID, Phase: "committed", ResourceID: id,
		Manager: req.Manager, Revision: &nextRev, Force: req.Force,
		Reason:  reason,
		Summary: summarizeChanges(mr.Changes),
		Detail:  mustJSON(map[string]any{"new_owners": mr.NewOwners})})

	return &ApplyOutcome{
		RunID:     runID,
		Revision:  nextRev,
		Live:      liveRaw,
		Changes:   mr.Changes,
		NewOwners: mr.NewOwners,
		Forced:    req.Force,
	}, nil
}

func categoryOf(err error) string {
	if se, ok := model.AsError(err); ok {
		return string(se.Category)
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return string(model.CatResourceExhausted)
	}
	return string(model.CatComputationFailure)
}

func codeOf(err error) string {
	if se, ok := model.AsError(err); ok {
		return se.Code
	}
	return "unknown"
}

func fmtErr(r any) string {
	switch v := r.(type) {
	case error:
		return v.Error()
	default:
		return jsonEscape(v)
	}
}

func jsonEscape(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func mustJSON(v any) json.RawMessage {
	b, _ := json.Marshal(v)
	return b
}

func summarizeChanges(cs model.ChangeSet) string {
	return jsonEscape(map[string]int{
		"added": len(cs.Added), "changed": len(cs.Changed), "removed": len(cs.Removed),
	})
}
