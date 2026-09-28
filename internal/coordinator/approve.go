package coordinator

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"evictor/internal/domain"
	"evictor/internal/logx"
	"evictor/internal/store"
)

// ApproveResult is returned for a successfully admitted eviction.
type ApproveResult struct {
	EvictionID string             `json:"eviction_id"`
	Group      string             `json:"group"`
	InstanceID string             `json:"instance_id"`
	ExpiresAt  time.Time          `json:"expires_at"`
	Budget     domain.Budget      `json:"budget"`
	Decision   domain.Decision    `json:"decision"`
}

// Approve admits one voluntary eviction.
//
// The flow is deliberately two-phase:
//
//  1. A REAL observation is pulled from the adapter and ingested. If the
//     adapter's sample is stale the answer is undecidable, never accept.
//  2. store.Approve begins ONE immediate transaction, re-reads every relevant
//     row INSIDE that transaction, re-runs domain.Admit against a budget that
//     includes all approved-but-unfinished evictions, and only then inserts
//     the approval row. Concurrent callers therefore serialize on SQLite's
//     write lock; whichever loses recomputes against the winner's committed
//     reservation and is rejected budget-exhausted or duplicate — there is no
//     read-then-commit window in which budget could be spent twice.
func (c *Coordinator) Approve(ctx context.Context, group, instanceID string, pinnedVersion int64, requestID string) (ApproveResult, error) {
	if requestID == "" {
		requestID = logx.NewRequestID()
	}
	now := c.cl.Now()

	// Phase 1: real, freshness-bounded observation.
	ob := c.obs.Observe(group, c.cfg.ObservationMaxAge)
	if len(ob.Instances) == 0 || !ob.Fresh(now) {
		age := time.Duration(0)
		if !ob.At.IsZero() {
			age = now.Sub(ob.At)
		}
		detail := fmt.Sprintf("observation age %s exceeds max %s; refusing to decide", age, c.cfg.ObservationMaxAge)
		c.audit(ctx, auditInput{
			TS: now, RequestID: requestID, Group: group, InstanceID: instanceID,
			Action: "approve", Outcome: "undecidable",
			Reason: domain.ReasonStaleObservation, Detail: detail,
		})
		c.log.Event("warn", "approve-undecidable-stale", requestID,
			logx.F("group", group), logx.F("instance", instanceID),
			logx.F("observation_age", age.String()),
			logx.F("max_age", c.cfg.ObservationMaxAge.String()))
		return ApproveResult{}, &RejectError{Reason: domain.ReasonStaleObservation, Detail: detail}
	}
	ing, err := c.st.IngestObservation(ctx, group, ob.At, observationRows(ob))
	if err != nil {
		return ApproveResult{}, fmt.Errorf("coordinator: ingest: %w", err)
	}

	// Phase 2: atomic admission + reservation.
	var result ApproveResult
	var decision domain.Decision
	approvalID := newEvictionID()
	pol0, err := c.policyFor(ctx, group)
	if err != nil {
		return ApproveResult{}, err
	}
	policyTTL := c.cfg.EvictionTTL
	if pol0.ApproveTTL > 0 {
		policyTTL = pol0.ApproveTTL
	}
	expiresAt := now.Add(policyTTL)

	_, err = c.st.Approve(ctx, store.ApproveInput{
		ID: approvalID, Group: group, InstanceID: instanceID,
		SelectorVersion: pinnedVersion, Now: now, ExpiresAt: expiresAt,
	}, func(q store.DBTX, txNow time.Time) error {
		st, err := store.ReadTxState(ctx, q, group)
		if err != nil {
			return err
		}
		// Freshness is re-checked on the persisted observation: an approval
		// can never commit against a sample older than the policy window even
		// if the adapter tick raced the reconcile loop.
		if st.LatestObservedAt.IsZero() || txNow.Sub(st.LatestObservedAt) > c.cfg.ObservationMaxAge {
			return &RejectError{Reason: domain.ReasonStaleObservation,
				Detail: "persisted observation is stale inside admission tx"}
		}
		pol := domain.Policy{
			Group:            st.Policy.Group,
			MinAvailable:     st.Policy.MinAvailable,
			MaxUnavailable:   st.Policy.MaxUnavailable,
			ApproveTTL:       st.Policy.ApproveTTL,
			CompletionTimeout: st.Policy.CompletionTimeout,
		}
		txOb := snapshotObservation(observationInput{
			Group: group, At: st.LatestObservedAt,
			MaxAge:  c.cfg.ObservationMaxAge,
			Instances: st.Instances,
		})
		approved := make([]domain.ApprovedEviction, 0, len(st.Approved))
		for _, a := range st.Approved {
			approved = append(approved, domain.ApprovedEviction{ID: a.ID, InstanceID: a.InstanceID})
		}
		budget, err := domain.EvaluateBudget(txOb, pol, approved)
		if err != nil {
			return fmt.Errorf("coordinator: budget: %w", err)
		}
		sel := domain.Selector{
			Group:       st.Selector.Group,
			Version:     st.Selector.Version,
			MatchLabels: st.Selector.MatchLabels,
			EffectiveAt: st.Selector.EffectiveAt,
		}
		decision = domain.Admit(domain.Request{
			InstanceID: instanceID, SelVersion: pinnedVersion, Now: txNow,
		}, txOb, sel, budget)
		if !decision.Accepted {
			return &RejectError{Reason: decision.Reason, Detail: decision.Detail, Budget: budget}
		}
		result = ApproveResult{
			EvictionID: approvalID, Group: group, InstanceID: instanceID,
			ExpiresAt: expiresAt, Budget: budget, Decision: decision,
		}
		return nil
	})
	if err != nil {
		var rej *RejectError
		if errors.As(err, &rej) {
			c.recordReject(ctx, requestID, group, instanceID, rej, now)
			return ApproveResult{}, rej
		}
		if errors.Is(err, store.ErrAlreadyOpen) {
			// The unique partial index fired: a concurrent request won the
			// same instance. That is the SAME category as draining.
			rej := &RejectError{Reason: domain.ReasonDuplicateEviction, Detail: err.Error()}
			c.recordReject(ctx, requestID, group, instanceID, rej, now)
			return ApproveResult{}, rej
		}
		return ApproveResult{}, fmt.Errorf("coordinator: approve: %w", err)
	}

	// Approval and reservation are committed. Ask the actuator to START the
	// voluntary move. A start failure cannot un-spend real budget silently;
	// the approval is explicitly failed with an involuntary-style reclaim
	// record (the drain never began) so the slot is released audibly.
	if d := c.drainer(group); d != nil {
		if !d.MarkDraining(instanceID) {
			detail := fmt.Sprintf("actuator refused to start drain for %s; releasing reservation", instanceID)
			if _, ferr := c.st.Fail(ctx, approvalID, detail, "approve:"+requestID, ing.ObservationID, now); ferr != nil {
				return ApproveResult{}, fmt.Errorf("coordinator: drain refused (%v) AND fail-settle failed: %w", err, ferr)
			}
			rej := &RejectError{Reason: domain.ReasonNotReady, Detail: detail, Budget: result.Budget}
			c.recordReject(ctx, requestID, group, instanceID, rej, now)
			return ApproveResult{}, rej
		}
	}

	c.audit(ctx, auditInput{
		TS: now, RequestID: requestID, Group: group, InstanceID: instanceID,
		Action: "approve", Outcome: "accepted",
		Reason: result.Decision.Reason, Detail: result.Decision.Detail,
		Budget: result.Budget, EvictionID: approvalID,
	})
	c.log.Event("info", "approve-accepted", requestID,
		logx.F("group", group), logx.F("instance", instanceID),
		logx.F("eviction", approvalID), logx.I64("selector_version", pinnedVersion),
		logx.I("budget_remaining", result.Budget.Remaining()),
		logx.I("budget_allowance", result.Budget.Allowance))
	return result, nil
}

func observationRows(ob domain.Observation) []store.ObservedInstance {
	rows := make([]store.ObservedInstance, 0, len(ob.Instances))
	for _, in := range ob.Instances {
		rows = append(rows, store.ObservedInstance{
			ID: in.ID, State: string(in.State),
			Labels: in.Labels, SelVersion: in.SelVersion,
		})
	}
	return rows
}

// snapshotObservation converts rows read inside the admission transaction
// into the domain observation the pure rule evaluates. Every instance
// carries the state from the LATEST persisted observation (see ReadTxState).
type observationInput struct {
	Group   string
	At      time.Time
	MaxAge  time.Duration
	Instances []store.InstanceRow
}

func snapshotObservation(in observationInput) domain.Observation {
	ob := domain.Observation{Group: in.Group, At: in.At, MaxAge: in.MaxAge}
	for _, x := range in.Instances {
		ob.Instances = append(ob.Instances, domain.Instance{
			ID:         x.ID,
			Group:      x.Group,
			Labels:     domain.ParseLabels(x.Labels),
			State:      domain.InstanceState(x.State),
			SelVersion: x.SelVersion,
		})
	}
	return ob
}

type auditInput struct {
	TS         time.Time
	RequestID  string
	Group      string
	InstanceID string
	Action     string
	Outcome    string
	Reason     string
	Detail     string
	Budget     domain.Budget
	EvictionID string
}

func (c *Coordinator) audit(ctx context.Context, in auditInput) {
	var budgetJSON string
	if in.Budget.Group != "" {
		b, _ := json.Marshal(in.Budget)
		budgetJSON = string(b)
	}
	_ = c.st.InsertDiagnostic(ctx, store.DiagnosticRecord{
		TS: in.TS, RequestID: in.RequestID, Group: in.Group,
		InstanceID: in.InstanceID, Action: in.Action, Outcome: in.Outcome,
		Reason: in.Reason, Detail: in.Detail, BudgetJSON: budgetJSON,
	})
}

func (c *Coordinator) recordReject(ctx context.Context, requestID, group, instanceID string, rej *RejectError, now time.Time) {
	outcome := "rejected"
	level := "warn"
	if rej.Reason == domain.ReasonStaleObservation {
		outcome = "undecidable"
	}
	if rej.Reason == domain.ReasonInvoluntaryFailure {
		level = "error"
	}
	c.audit(ctx, auditInput{
		TS: now, RequestID: requestID, Group: group, InstanceID: instanceID,
		Action: "approve", Outcome: outcome,
		Reason: rej.Reason, Detail: rej.Detail, Budget: rej.Budget,
	})
	c.log.Event(level, "approve-"+outcome, requestID,
		logx.F("group", group), logx.F("instance", instanceID),
		logx.F("reason", rej.Reason), logx.F("detail", rej.Detail))
}

func newEvictionID() string {
	var b [10]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "evc-fallback"
	}
	return "evc-" + hex.EncodeToString(b[:])
}

// policyFor reads one group's policy row.
func (c *Coordinator) policyFor(ctx context.Context, group string) (store.PolicyRow, error) {
	st, err := c.st.LoadSnapshot(ctx, group)
	if err != nil {
		return store.PolicyRow{}, err
	}
	return st.Policy, nil
}
