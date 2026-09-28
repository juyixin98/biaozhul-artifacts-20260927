package coordinator

import (
	"context"
	"errors"
	"time"

	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/store"
)

// ReconcileResult is one sweep's outcome.
type ReconcileResult struct {
	Expired []domain.Approval
	At      time.Time
}

// SweepExpiries marks pending approvals past their deadline as expired. It does
// NOT release their budget: expired approvals stay charged until an operator (or
// the owning client) explicitly confirms reclamation. This is the "revocation
// requires acknowledgement" rule.
func (c *Coordinator) SweepExpiries(ctx context.Context) (ReconcileResult, error) {
	now := c.cfg.Clock()
	exp, err := c.st.MarkExpired(ctx, now)
	return ReconcileResult{Expired: exp, At: now}, err
}

// ReportResult is a client telling the coordinator an approved eviction ran
// (succeeded) or could not run (failed). Both outcomes free the reservation.
func (c *Coordinator) ReportResult(ctx context.Context, approvalID string, succeeded bool, reason string) (domain.Approval, error) {
	state := domain.ApprovalSucceeded
	if !succeeded {
		state = domain.ApprovalFailed
	}
	a, err := c.st.SetApprovalResult(ctx, approvalID, state, reason, c.cfg.Clock())
	if errors.Is(err, store.ErrStateConflict) {
		return a, ErrLifecycle
	}
	return a, err
}

// Reclaim releases the reservation of an expired/stale approval. The caller
// must echo the approval id as confirmation: a stale approval cannot be
// silently refunded by a background loop.
func (c *Coordinator) Reclaim(ctx context.Context, approvalID, confirmation string) (domain.Approval, error) {
	a, err := c.st.ReclaimApproval(ctx, approvalID, confirmation, c.cfg.Clock())
	switch {
	case errors.Is(err, store.ErrConfirmationRequired):
		return a, ErrConfirmationRequired
	case errors.Is(err, store.ErrStateConflict):
		return a, ErrLifecycle
	case errors.Is(err, store.ErrNotFound):
		return a, ErrApprovalNotFound
	}
	return a, err
}

// GetApproval exposes the approval for diagnostics.
func (c *Coordinator) GetApproval(ctx context.Context, id string) (domain.Approval, error) {
	return c.st.GetApproval(ctx, id)
}

// Reclaimable lists reservations awaiting explicit reclamation.
func (c *Coordinator) Reclaimable(ctx context.Context, namespace, group string) ([]domain.Approval, error) {
	return c.st.ListReclaimable(ctx, namespace, group)
}

// ChangeSelector versions a selector change and immediately renders pending
// approvals from the old epoch stale (but still charged until reclaimed).
// It returns the updated group, the OLD epoch and the NEW epoch.
func (c *Coordinator) ChangeSelector(ctx context.Context, namespace, group, _ string) (updated domain.Group, oldEpoch, newEpoch int64, err error) {
	return c.st.BumpSelectorEpoch(ctx, namespace, group)
}

// Sentinel lifecycle errors for the HTTP layer.
var (
	ErrLifecycle            = errors.New("coordinator: approval lifecycle conflict")
	ErrConfirmationRequired = errors.New("coordinator: reclaim confirmation required")
	ErrApprovalNotFound     = errors.New("coordinator: approval not found")
)

// StartLoop runs the expiry sweep on an interval until ctx is cancelled. It is
// the coordination loop: the only background responsibility, kept deliberately
// small so its behaviour is fully testable.
func (c *Coordinator) StartLoop(ctx context.Context, interval time.Duration) <-chan ReconcileResult {
	out := make(chan ReconcileResult, 4)
	go func() {
		defer close(out)
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				res, err := c.SweepExpiries(ctx)
				if err != nil {
					continue
				}
				select {
				case out <- res:
				default:
				}
			}
		}
	}()
	return out
}
