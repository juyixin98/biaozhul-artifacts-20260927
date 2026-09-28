package coordinator

import (
	"context"
	"time"

	"evictor/internal/domain"
	"evictor/internal/store"
)

// ProvisionGroupInput creates a group end to end: policy row, selector v1 and
// topology in both the persistent store and the synthetic cluster.
type ProvisionGroupInput struct {
	Group           string
	MinAvailable    int
	MaxUnavailable  int
	ApproveTTL      time.Duration
	CompleteTimeout time.Duration
	MatchLabels     map[string]string
	InstanceIDs     []string
}

// ProvisionStore writes the policy/selector/topology rows alone (used by
// tests that drive the adapter separately).
func (c *Coordinator) ProvisionStore(ctx context.Context, in ProvisionGroupInput) error {
	if err := c.st.UpsertPolicy(ctx, store.PolicyRow{
		Group: in.Group, MinAvailable: in.MinAvailable,
		MaxUnavailable: in.MaxUnavailable,
		ApproveTTL: in.ApproveTTL, CompletionTimeout: in.CompleteTimeout,
	}); err != nil {
		return err
	}
	labels := domain.CanonicalLabels(in.MatchLabels)
	if _, _, err := c.st.PublishSelector(ctx, in.Group, labels, c.cl.Now()); err != nil {
		return err
	}
	for _, id := range in.InstanceIDs {
		if err := c.st.EnsureInstance(ctx, id, in.Group, labels, 1); err != nil {
			return err
		}
	}
	return nil
}
