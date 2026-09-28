package controller

import (
	"context"
	"errors"
	"fmt"

	"rollctl/internal/model"
	"rollctl/internal/store"
)

// CreateWorkloadInput is the request to register a workload.
type CreateWorkloadInput struct {
	Name      string
	Replicas  int
	Revision  string
	Policy    model.Policy
	RequestID string
}

// CreateWorkload registers a workload. A bootstrap release row is written at
// creation and immediately marked succeeded so the history shows what the
// workload started on. No instances exist yet; the reconcile loop grows the
// fleet itself (steady state), so creation never fakes readiness.
func (c *Controller) CreateWorkload(ctx context.Context, in CreateWorkloadInput) (*model.Workload, error) {
	if in.Name == "" {
		return nil, fmt.Errorf("%w: name required", ErrBadRequest)
	}
	if in.Replicas <= 0 {
		return nil, fmt.Errorf("%w: replicas must be positive", ErrBadRequest)
	}
	if in.Revision == "" {
		return nil, fmt.Errorf("%w: revision required", ErrBadRequest)
	}
	p := withDefaults(in.Policy)
	if err := validatePolicy(p); err != nil {
		return nil, err
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	if _, err := c.st.GetWorkload(ctx, in.Name); err == nil {
		return nil, fmt.Errorf("%w: workload %q already exists", ErrBadRequest, in.Name)
	} else if !errors.Is(err, store.ErrNotFound) {
		return nil, err
	}

	now := c.now()
	rel := &model.Release{
		ID: genID("r"), Workload: in.Name, Revision: in.Revision,
		State: model.RelSucceeded, Kind: model.KindBootstrap, Policy: p,
		RequestID: in.RequestID, CreatedAt: now,
	}
	w := &model.Workload{
		Name: in.Name, Replicas: in.Replicas, CurrentRevision: in.Revision,
		CurrentReleaseID: rel.ID, CreatedAt: now,
	}
	if err := c.st.CreateWorkload(ctx, w, rel, in.RequestID); err != nil {
		return nil, err
	}
	ev := c.mkEvent(0, in.Name, rel, model.LevelInfo, model.EvReleaseCreated,
		"workload created on revision %s with %d replicas (bootstrap release %s)", in.Revision, in.Replicas, rel.ID)
	if _, err := c.st.AppendEvent(ctx, ev); err != nil {
		return nil, err
	}
	return w, nil
}

// CreateReleaseInput requests a new rollout.
type CreateReleaseInput struct {
	Workload  string
	Revision  string
	Policy    *model.Policy // optional; workload/bootstrap policy default
	RequestID string
}

// CreateRelease records a pending rollout. It is rejected while another
// release is pending/active (409 semantics).
func (c *Controller) CreateRelease(ctx context.Context, in CreateReleaseInput) (*model.Release, error) {
	if in.Workload == "" || in.Revision == "" {
		return nil, fmt.Errorf("%w: workload and revision required", ErrBadRequest)
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	w, err := c.st.GetWorkload(ctx, in.Workload)
	if errors.Is(err, store.ErrNotFound) {
		return nil, ErrWorkloadNotFound
	}
	if err != nil {
		return nil, err
	}
	if busy, _ := c.st.PendingOrActiveRelease(ctx, in.Workload); busy != nil {
		return nil, fmt.Errorf("%w: release %s", ErrActiveRelease, busy.ID)
	}
	var p model.Policy
	if in.Policy != nil {
		p = withDefaults(*in.Policy)
		if err := validatePolicy(p); err != nil {
			return nil, err
		}
	} else {
		last, lerr := c.st.LatestFinishedRelease(ctx, in.Workload)
		if lerr != nil {
			return nil, lerr
		}
		if last != nil {
			p = withDefaults(last.Policy)
		} else {
			p = DefaultPolicy()
		}
	}

	rel := &model.Release{
		ID: genID("r"), Workload: in.Workload, Revision: in.Revision,
		PreviousReleaseID: w.CurrentReleaseID,
		State:             model.RelPending, Kind: model.KindRollout, Policy: p,
		RequestID: in.RequestID, CreatedAt: c.now(),
	}
	if err := c.st.InsertRelease(ctx, rel); err != nil {
		return nil, err
	}
	ev := c.mkEvent(0, in.Workload, rel, model.LevelInfo, model.EvReleaseCreated,
		"rollout requested: %s -> %s (release %s, request_id=%s)", w.CurrentRevision, in.Revision, rel.ID, in.RequestID)
	if _, err := c.st.AppendEvent(ctx, ev); err != nil {
		return nil, err
	}
	return rel, nil
}

// RollbackInput requests a rollback. TargetRevision may be empty, in which
// case the most recent earlier SUCCEEDED revision is chosen. Rollback always
// creates a brand-new release (KindRollback) and preserves all history.
type RollbackInput struct {
	Workload       string
	TargetRevision string
	RequestID      string
	Policy         *model.Policy
}

// Rollback creates the rollback release and returns it. A rollback to the
// revision currently serving all traffic is resolved by the reconcile loop as
// an immediate no-diff success.
func (c *Controller) Rollback(ctx context.Context, in RollbackInput) (*model.Release, error) {
	if in.Workload == "" {
		return nil, fmt.Errorf("%w: workload required", ErrBadRequest)
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	w, err := c.st.GetWorkload(ctx, in.Workload)
	if errors.Is(err, store.ErrNotFound) {
		return nil, ErrWorkloadNotFound
	}
	if err != nil {
		return nil, err
	}
	if busy, _ := c.st.PendingOrActiveRelease(ctx, in.Workload); busy != nil {
		return nil, fmt.Errorf("%w: release %s", ErrActiveRelease, busy.ID)
	}

	target := in.TargetRevision
	var rollbackOf string
	if target == "" {
		// Walk history newest-first: skip failed releases and the currently
		// serving revision; choose the first other succeeded revision.
		hist, herr := c.st.ListReleases(ctx, in.Workload)
		if herr != nil {
			return nil, herr
		}
		for _, h := range hist {
			if h.State != model.RelSucceeded {
				continue
			}
			if rollbackOf == "" && h.Revision != w.CurrentRevision {
				rollbackOf = h.ID
			}
			if h.Revision != w.CurrentRevision {
				target = h.Revision
				break
			}
		}
		if target == "" {
			return nil, ErrNoRollbackTarget
		}
	} else {
		hist, herr := c.st.ListReleases(ctx, in.Workload)
		if herr != nil {
			return nil, herr
		}
		found := false
		for _, h := range hist {
			if h.State == model.RelSucceeded && h.Revision == target {
				found = true
				if rollbackOf == "" {
					rollbackOf = h.ID
				}
			}
		}
		if !found {
			return nil, fmt.Errorf("%w: revision %s never succeeded on this workload", ErrNoRollbackTarget, target)
		}
	}

	var p model.Policy
	if in.Policy != nil {
		p = withDefaults(*in.Policy)
		if err := validatePolicy(p); err != nil {
			return nil, err
		}
	} else {
		last, lerr := c.st.LatestFinishedRelease(ctx, in.Workload)
		if lerr != nil {
			return nil, lerr
		}
		if last != nil {
			p = withDefaults(last.Policy)
		} else {
			p = DefaultPolicy()
		}
	}

	rel := &model.Release{
		ID: genID("r"), Workload: in.Workload, Revision: target,
		PreviousReleaseID: w.CurrentReleaseID,
		State:             model.RelPending, Kind: model.KindRollback, Policy: p,
		RollbackOf: rollbackOf, RequestID: in.RequestID, CreatedAt: c.now(),
	}
	if err := c.st.InsertRelease(ctx, rel); err != nil {
		return nil, err
	}
	ev := c.mkEvent(0, in.Workload, rel, model.LevelInfo, model.EvReleaseCreated,
		"rollback requested to revision %s (rollback release %s of %s, request_id=%s)",
		target, rel.ID, orDefault(rollbackOf, "<current>"), in.RequestID)
	if _, err := c.st.AppendEvent(ctx, ev); err != nil {
		return nil, err
	}
	return rel, nil
}

// PolicyInput carries policy overrides from the API. Nil fields fall back to
// defaults; a pointer to zero is an explicit zero (so surge=0/ unavailable=0,
// a provably blocked policy, can be requested as a fixture).
type PolicyInput struct {
	MaxSurge            *int   `json:"maxSurge,omitempty"`
	MaxUnavailable      *int   `json:"maxUnavailable,omitempty"`
	ReadyThresholdTicks *int   `json:"readyThresholdTicks,omitempty"`
	DeadlineTicks       *int64 `json:"deadlineTicks,omitempty"`
	MaxStartFailures    *int   `json:"maxStartFailures,omitempty"`
}

// Resolve applies overrides over the healthy default policy.
func (pi *PolicyInput) Resolve() model.Policy {
	p := DefaultPolicy()
	if pi != nil {
		if pi.MaxSurge != nil {
			p.MaxSurge = *pi.MaxSurge
		}
		if pi.MaxUnavailable != nil {
			p.MaxUnavailable = *pi.MaxUnavailable
		}
		if pi.ReadyThresholdTicks != nil {
			p.ReadyThresholdTicks = *pi.ReadyThresholdTicks
		}
		if pi.DeadlineTicks != nil {
			p.DeadlineTicks = *pi.DeadlineTicks
		}
		if pi.MaxStartFailures != nil {
			p.MaxStartFailures = *pi.MaxStartFailures
		}
	}
	return p
}

// DefaultPolicy is the healthy policy used when no policy is supplied.
func DefaultPolicy() model.Policy {
	return model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 20, MaxStartFailures: 0}
}

// withDefaults normalizes a policy accepted at the controller boundary. The Go
// zero-value policy means "nothing supplied" and becomes the healthy default;
// explicit zeros on surge/unavailable with the other fields set are preserved
// (surge=0/ unavailable=0 is a valid but provably blocked fixture policy).
func withDefaults(p model.Policy) model.Policy {
	if p == (model.Policy{}) {
		return DefaultPolicy()
	}
	if p.MaxSurge < 0 {
		p.MaxSurge = 0
	}
	if p.MaxUnavailable < 0 {
		p.MaxUnavailable = 0
	}
	if p.ReadyThresholdTicks <= 0 {
		p.ReadyThresholdTicks = 2
	}
	if p.DeadlineTicks <= 0 {
		p.DeadlineTicks = 20
	}
	if p.MaxStartFailures < 0 {
		p.MaxStartFailures = 0
	}
	return p
}

func validatePolicy(p model.Policy) error {
	if p.MaxSurge < 0 || p.MaxUnavailable < 0 {
		return fmt.Errorf("%w: surge and unavailable must be non-negative", ErrBadRequest)
	}
	if p.ReadyThresholdTicks <= 0 {
		return fmt.Errorf("%w: readyThresholdTicks must be positive", ErrBadRequest)
	}
	if p.DeadlineTicks <= 0 {
		return fmt.Errorf("%w: deadlineTicks must be positive", ErrBadRequest)
	}
	return nil
}
