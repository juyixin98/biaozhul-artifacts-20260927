// Package coordinator is the reconciliation loop and decision engine. It turns
// registered facts (groups, readiness observations, involuntary failures) and
// live approvals into accept/reject/cannot-decide outcomes.
//
// The hard guarantee lives here: reading the budget state and inserting the
// approval happen in ONE SQLite BEGIN IMMEDIATE transaction, so concurrent
// requests can never reserve the same budget slot twice.
package coordinator

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/local/evictioncoordinator/internal/budget"
	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/store"
)

// Clock is injectable so tests can drive expiry deterministically.
type Clock func() time.Time

// Config tunes the coordinator.
type Config struct {
	ApprovalTTL time.Duration
	Clock       Clock
}

// Coordinator wires the store to the pure budget engine.
type Coordinator struct {
	st  *store.Store
	cfg Config
}

// New builds a coordinator with sane defaults.
func New(st *store.Store, cfg Config) *Coordinator {
	if cfg.ApprovalTTL <= 0 {
		cfg.ApprovalTTL = 30 * time.Second
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	return &Coordinator{st: st, cfg: cfg}
}

// Request is one voluntary eviction request.
type Request struct {
	Namespace  string
	Group      string
	InstanceID string
	// RequestID is the caller's correlation/idempotency key (e.g. HTTP
	// X-Request-ID). Empty means the coordinator mints one.
	RequestID string
	// ClientEpoch, when set, records the selector epoch the client observed; a
	// stale client epoch yields CatStaleEpoch instead of a silent decision.
	ClientEpoch int64
	HasEpoch    bool
}

func newRequestID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return "req-" + hex.EncodeToString(b[:])
}

func newApprovalID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return "appr-" + hex.EncodeToString(b[:])
}

// reject builds a non-acceptance decision.
func reject(reqID string, g domain.Group, snap domain.BudgetSnapshot, instanceID string, cat domain.FailureCategory, format string, args ...any) domain.Decision {
	if snap.Group == "" {
		snap.Group = g.Key()
	}
	if snap.SelectorEpoch == 0 {
		snap.SelectorEpoch = g.SelectorEpoch
	}
	snap.DesiredReplicas = g.Replicas
	return domain.Decision{
		RequestID: reqID,
		Group:     g.Key(),
		Instance:  instanceID,
		Category:  cat,
		Reason:    fmt.Sprintf(format, args...),
		Snapshot:  snap,
	}
}

// Evict runs the atomic reserve-or-reject decision.
func (c *Coordinator) Evict(ctx context.Context, req Request) (domain.Decision, error) {
	reqID := req.RequestID
	if reqID == "" {
		reqID = newRequestID()
	}
	now := c.cfg.Clock()
	var decision domain.Decision

	txErr := c.st.WithImmediateTx(ctx, func(q store.TX) error {
		// Idempotency: replaying the same request id returns the original
		// recorded decision instead of reserving a second slot.
		if prior, ok, err := c.st.GetDecisionOn(ctx, q, reqID); err != nil {
			return err
		} else if ok {
			decision = prior
			return nil
		}
		g, err := c.st.GetGroupOn(ctx, q, req.Namespace, req.Group)
		if errors.Is(err, store.ErrNotFound) {
			decision = reject(reqID, domain.Group{Namespace: req.Namespace, Name: req.Group},
				domain.BudgetSnapshot{Group: req.Namespace + "/" + req.Group},
				req.InstanceID, domain.CatGroupNotFound, "group %s/%s is not registered", req.Namespace, req.Group)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}
		if err != nil {
			return err
		}

		// A client acting on an old selector version cannot get a decision.
		if req.HasEpoch && req.ClientEpoch != 0 && req.ClientEpoch < g.SelectorEpoch {
			decision = reject(reqID, g, domain.BudgetSnapshot{Group: g.Key(), SelectorEpoch: g.SelectorEpoch},
				req.InstanceID, domain.CatStaleEpoch,
				"client selector epoch %d is older than current epoch %d; re-list before evicting",
				req.ClientEpoch, g.SelectorEpoch)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}

		// Membership is derived against the CURRENT selector version only.
		inst, err := c.st.InstanceOn(ctx, q, req.InstanceID)
		if errors.Is(err, store.ErrNotFound) {
			decision = reject(reqID, g, domain.BudgetSnapshot{Group: g.Key(), SelectorEpoch: g.SelectorEpoch},
				req.InstanceID, domain.CatInstanceNotMember, "instance %s is not registered", req.InstanceID)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}
		if err != nil {
			return err
		}
		if inst.Namespace != req.Namespace || inst.Group != req.Group ||
			!domain.MatchSelector(g.SelectorLabels, inst.Labels) {
			decision = reject(reqID, g, domain.BudgetSnapshot{Group: g.Key(), SelectorEpoch: g.SelectorEpoch},
				req.InstanceID, domain.CatInstanceNotMember,
				"instance %s does not match selector %v at epoch %d",
				req.InstanceID, redactLabels(g.SelectorLabels), g.SelectorEpoch)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}

		// Reconcile every current member from real facts.
		members, err := c.st.MemberInstancesOn(ctx, q, req.Namespace, req.Group)
		if err != nil {
			return err
		}
		pending, err := c.st.PendingApprovalsOn(ctx, q, req.Namespace, req.Group)
		if err != nil {
			return err
		}
		pendingByInstance := map[string]domain.Approval{}
		for _, a := range pending {
			if a.Epoch == g.SelectorEpoch {
				pendingByInstance[a.InstanceID] = a
			}
		}

		// charged includes expired/stale reservations that have NOT been
		// reclaimed: their budget slot remains occupied across epoch/expiry.
		charged, err := c.st.ChargedApprovalsOn(ctx, q, req.Namespace, req.Group)
		if err != nil {
			return err
		}
		chargedByInstance := map[string]domain.Approval{}
		var legacyCharged int32
		for _, a := range charged {
			if a.Epoch == g.SelectorEpoch {
				chargedByInstance[a.InstanceID] = a
			} else {
				// Reservation from an older selector epoch: unattributable to a
				// current member but still consuming group budget until reclaimed.
				legacyCharged++
			}
		}

		states := make([]budget.InstanceState, 0, len(members))
		var target budget.InstanceState
		targetFound := false
		for _, m := range members {
			if !domain.MatchSelector(g.SelectorLabels, m.Labels) {
				continue
			}
			st := budget.InstanceState{InstanceID: m.ID}
			obs, hasObs, err := c.st.LatestObservationOn(ctx, q, m.ID)
			if err != nil {
				return err
			}
			failed, failReason, err := c.st.FailureAtEpochOn(ctx, q, m.ID, g.SelectorEpoch)
			if err != nil {
				return err
			}
			_ = failReason
			switch {
			case !hasObs:
				// no observation at all -> unknown
			case obs.Epoch != g.SelectorEpoch:
				// observation from an older selector version -> must not be used
			default:
				st.Observed = true
				st.Ready = obs.Ready
			}
			st.Failed = failed
			if a, ok := pendingByInstance[m.ID]; ok {
				aa := a
				st.LiveApproval = &aa
			}
			if a, ok := chargedByInstance[m.ID]; ok {
				aa := a
				st.ChargedApproval = &aa
			}
			states = append(states, st)
			if m.ID == req.InstanceID {
				target, targetFound = st, true
			}
		}
		if !targetFound {
			decision = reject(reqID, g, domain.BudgetSnapshot{Group: g.Key(), SelectorEpoch: g.SelectorEpoch},
				domain.CatInstanceNotMember, "instance %s absent from current selector membership", req.InstanceID)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}

		// Declared replicas are a membership floor: if fewer members are known
		// than the group's desired size, the missing members are treated as
		// unknown (no observation) rather than ignored, so a partial view can
		// never produce an optimistic approval.
		for int32(len(states)) < g.Replicas {
			states = append(states, budget.InstanceState{
				InstanceID: fmt.Sprintf("__unknown_%d", len(states)),
			})
		}

		snap := budget.Compute(g, states, legacyCharged)

		// Ordering of refusal categories is deliberate:
		// 1. involuntary failure is a fact, never a budget-blockable request;
		// 2. stale observations -> cannot decide, not a rejection;
		// 3. existing live approval -> no double reservation;
		// 4. the budget itself.
		if target.Failed {
			decision = reject(reqID, g, snap, domain.CatInstanceFailed,
				"instance %s has an involuntary failure recorded at epoch %d; a voluntary eviction cannot proceed and the failure is not counted as a budget-blocked disruption",
				req.InstanceID, g.SelectorEpoch)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}
		if !target.Observed {
			decision = reject(reqID, g, snap, domain.CatUnknownReadiness,
				"no current-epoch (epoch=%d) readiness observation for instance %s; cannot decide safely",
				g.SelectorEpoch, req.InstanceID)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}
		if target.LiveApproval != nil {
			decision = reject(reqID, g, snap, domain.CatAlreadyEvicting,
				"instance %s already holds pending approval %s; budget slot is not consumed twice",
				req.InstanceID, target.LiveApproval.ID)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}
		if !snap.Fresh {
			decision = reject(reqID, g, snap, domain.CatUnknownReadiness,
				"group observation set is stale: %d member(s) lack current-epoch readiness; cannot decide",
				snap.UnknownReplicas)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}
		if !budget.CanApprove(snap, target) {
			decision = reject(reqID, g, snap, domain.CatBudgetExhausted,
				"no budget slot: allowed_unavailable=%d current_unavailable=%d charged_approvals=%d ready=%d/%d",
				snap.AllowedUnavailable, snap.CurrentUnavailable, snap.ChargedApprovals,
				snap.ReadyReplicas, snap.DesiredReplicas)
			return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
		}

		// ACCEPT: insert the approval in the SAME transaction that read the free
		// slot. Any concurrent request waits on the write lock and recomputes.
		approval := domain.Approval{
			ID:         newApprovalID(),
			Namespace:  g.Namespace,
			Group:      g.Name,
			InstanceID: req.InstanceID,
			Epoch:      g.SelectorEpoch,
			State:      domain.ApprovalPending,
			ReservedAt: now,
			ExpiresAt:  now.Add(c.cfg.ApprovalTTL),
		}
		if err := c.st.InsertApproval(ctx, q, approval); err != nil {
			return err
		}
		snap.ChargedApprovals++
		snap.CurrentUnavailable++
		if snap.AvailableSlots > 0 {
			snap.AvailableSlots--
		}
		decision = domain.Decision{
			RequestID:  reqID,
			Group:      g.Key(),
			Instance:   req.InstanceID,
			Accepted:   true,
			ApprovalID: approval.ID,
			Reason: fmt.Sprintf("approved: reserved 1 of %d interruption slot(s) at epoch %d; expires %s",
				snap.AllowedUnavailable, g.SelectorEpoch, approval.ExpiresAt.UTC().Format(time.RFC3339)),
			Snapshot: snap,
		}
		return c.st.InsertDecision(ctx, q, decision, req.InstanceID, now)
	})
	if txErr != nil {
		return domain.Decision{}, txErr
	}
	return decision, nil
}

// redactLabels renders selector labels for diagnostics without exposing label
// values that might carry workload-specific data: only keys are printed.
func redactLabels(m map[string]string) map[string]string {
	out := make(map[string]string, len(m))
	for k := range m {
		out[k] = "<redacted>"
	}
	return out
}
