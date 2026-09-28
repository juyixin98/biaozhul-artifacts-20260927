package nat

import (
	"context"

	"natlab/internal/model"
)

// ListEvents returns the persisted decision log for a run.
func (e *Engine) ListEvents(ctx context.Context, runID string, limit int) ([]*model.Event, error) {
	return e.store.ListEvents(ctx, runID, limit)
}

// ListMappings returns mappings for a run.
func (e *Engine) ListMappings(ctx context.Context, runID string, activeOnly bool) ([]*model.Mapping, error) {
	return e.store.ListMappings(ctx, runID, activeOnly)
}
