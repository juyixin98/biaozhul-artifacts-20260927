package coordinator

import (
	"context"
	"fmt"
	"time"

	"evictor/internal/domain"
	"evictor/internal/logx"
	"evictor/internal/store"
)

// PublishSelector publishes a selector version for a group. When the label
// expression actually changes:
//
//  1. a NEW monotonic version is inserted and the old one marked superseded
//     (selector changes are versioned — never mutated in place);
//  2. every open approval pinned to a superseded version is revoked INSIDE
//     THE SAME outer service flow, each producing an explicit
//     "selector-change" reclaim confirmation record.
//
// Returning the version lets callers (and tests) pin subsequent requests.
func (c *Coordinator) PublishSelector(ctx context.Context, group, canonicalLabels, requestID string) (version int64, created bool, err error) {
	if requestID == "" {
		requestID = logx.NewRequestID()
	}
	now := c.cl.Now()
	sel, created, err := c.st.PublishSelector(ctx, group, canonicalLabels, now)
	if err != nil {
		return 0, false, fmt.Errorf("coordinator: publish selector: %w", err)
	}
	if !created {
		return sel.Version, false, nil
	}
	// Invalidate approvals issued against older selections. Revocation is
	// itself the explicit reclamation: each revoked approval leaves a
	// reclaim_events row (kind=selector-change) that the owner must be able
	// to retrieve through the confirmation endpoint — budget is never freed
	// by silently deleting the approval.
	open, err := c.st.OpenEvictionsForVersions(ctx, group, sel.Version)
	if err != nil {
		return sel.Version, true, err
	}
	for _, e := range open {
		detail := fmt.Sprintf("selector for group %s moved v%d -> v%d; approval pinned to v%d invalidated",
			group, e.SelectorVersion, sel.Version, e.SelectorVersion)
		if _, err := c.st.Revoke(ctx, e.ID, detail, "selector-change:"+requestID, now); err != nil {
			return sel.Version, true, fmt.Errorf("coordinator: revoke %s: %w", e.ID, err)
		}
		c.audit(ctx, auditInput{
			TS: now, RequestID: requestID, Group: group, InstanceID: e.InstanceID,
			Action: "revoke-selector-change", Outcome: "revoked",
			Reason: "rejected:selector-version-superseded", Detail: detail,
		})
		c.log.Event("warn", "eviction-revoked-selector", requestID,
			logx.F("group", group), logx.F("eviction", e.ID),
			logx.I64("pinned_version", e.SelectorVersion),
			logx.I64("current_version", sel.Version))
	}
	c.log.Event("info", "selector-published", requestID,
		logx.F("group", group), logx.I64("version", sel.Version),
		logx.S("labels", canonicalLabels))
	return sel.Version, true, nil
}

// ConfirmReclaim is the explicit reclamation confirmation entry point. It is
// idempotent: a terminal eviction already carries its reclaim confirmation
// record(s), which are returned for the owner to acknowledge. An eviction
// that is still open returns ReasonEvictionStillOpen so the caller can
// distinguish "nothing to confirm yet" from "budget released, here is proof".
func (c *Coordinator) ConfirmReclaim(ctx context.Context, evictionID, confirmedBy string) (ReclaimSummary, error) {
	now := c.cl.Now()
	e, err := c.st.Eviction(ctx, evictionID)
	if err != nil {
		return ReclaimSummary{}, err
	}
	if e.Phase == string(domain.PhaseApproved) {
		return ReclaimSummary{
			EvictionID: evictionID, Phase: e.Phase, Open: true,
		}, &RejectError{Reason: domain.ReasonEvictionStillOpen,
			Detail: "eviction is still approved; no budget has been reclaimed yet"}
	}
	events, err := c.st.ReclaimEvents(ctx, evictionID)
	if err != nil {
		return ReclaimSummary{}, err
	}
	out := ReclaimSummary{
		EvictionID: evictionID, Phase: e.Phase, Open: false,
		OutcomeReason: e.OutcomeReason, ConfirmedBy: confirmedBy, ConfirmedAt: now,
	}
	for _, rc := range events {
		out.Events = append(out.Events, ReclaimEvent{
			Kind: rc.Kind, Detail: rc.Detail,
			RecordedAt: rc.ConfirmedAt, RecordedBy: rc.ConfirmedBy,
		})
	}
	return out, nil
}

// ReclaimEvent is one confirmation artifact returned to owners.
type ReclaimEvent struct {
	Kind       string    `json:"kind"`
	Detail     string    `json:"detail"`
	RecordedAt time.Time `json:"recorded_at"`
	RecordedBy string    `json:"recorded_by"`
}

// ReclaimSummary answers a confirm request: either "still open, nothing to
// reclaim" or the terminal phase plus the persisted confirmations.
type ReclaimSummary struct {
	EvictionID    string         `json:"eviction_id"`
	Phase         string         `json:"phase"`
	Open          bool           `json:"open"`
	OutcomeReason string         `json:"outcome_reason,omitempty"`
	Events        []ReclaimEvent `json:"events,omitempty"`
	ConfirmedBy   string         `json:"confirmed_by,omitempty"`
	ConfirmedAt   time.Time      `json:"confirmed_at,omitempty"`
}

// BudgetView is the read model served to operators: the current selector,
// freshness status and the fully itemized evaluated budget.
type BudgetView struct {
	Group        string        `json:"group"`
	ObservedAt   time.Time     `json:"observed_at"`
	Fresh        bool          `json:"fresh"`
	SelectorV    int64         `json:"selector_version"`
	SelectorExpr string        `json:"selector_labels"`
	Budget       domain.Budget `json:"budget"`
}

// Snapshot evaluates and returns the current budget view of a group from the
// LATEST persisted observation (it does not itself pull from the adapter;
// call Reconcile first or let the loop do it).
func (c *Coordinator) Snapshot(ctx context.Context, group string) (BudgetView, error) {
	now := c.cl.Now()
	st, err := c.st.LoadSnapshot(ctx, group)
	if err != nil {
		return BudgetView{}, err
	}
	ob := txObservation(st, now, c.cfg.ObservationMaxAge)
	pol := domain.Policy{
		Group: group,
		MinAvailable:     st.Policy.MinAvailable,
		MaxUnavailable:   st.Policy.MaxUnavailable,
		ApproveTTL:       st.Policy.ApproveTTL,
		CompletionTimeout: st.Policy.CompletionTimeout,
	}
	open, err := c.st.ApprovedEvictions(ctx, group)
	if err != nil {
		return BudgetView{}, err
	}
	approved := make([]domain.ApprovedEviction, 0, len(open))
	for _, e := range open {
		approved = append(approved, domain.ApprovedEviction{ID: e.ID, InstanceID: e.InstanceID})
	}
	b, err := domain.EvaluateBudget(ob, pol, approved)
	if err != nil {
		return BudgetView{}, err
	}
	return BudgetView{
		Group: group, ObservedAt: st.LatestObservedAt,
		Fresh: !st.LatestObservedAt.IsZero() && now.Sub(st.LatestObservedAt) <= c.cfg.ObservationMaxAge,
		SelectorV: st.Selector.Version, SelectorExpr: st.Selector.MatchLabels, Budget: b,
	}, nil
}
