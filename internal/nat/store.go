package nat

import (
	"context"
	"time"

	"natlab/internal/model"
)

// StateStore is the engine's persistence contract. SQLite implements it in
// the store package; tests may substitute an in-memory fake. Time filtering
// is the engine's job: callers pass now explicitly, and mappings returned by
// ActiveByFlow/ActiveByExtPort are guaranteed un-expired at that instant.
type StateStore interface {
	// EnsureRun creates the run row if missing and returns the clock
	// high-water mark persisted for it (zero time for a brand-new run).
	EnsureRun(ctx context.Context, runID string) (highWater time.Time, err error)

	// SweepExpired moves mappings with expires_at <= now to closed and returns
	// the rows it closed. The engine releases their ports.
	SweepExpired(ctx context.Context, runID string, now time.Time) (closed []*model.Mapping, err error)

	// ActiveByFlow fetches the un-expired mapping for the full 5-tuple flow.
	ActiveByFlow(ctx context.Context, runID string, k model.FlowKey, now time.Time) (*model.Mapping, error)

	// ActiveByExtPort fetches the un-expired mapping owning mappedPort for the
	// protocol (used for inbound packets).
	ActiveByExtPort(ctx context.Context, runID string, proto model.Protocol, mappedPort uint16, now time.Time) (*model.Mapping, error)

	// HistoryByExtPort returns the most recent mapping (active or closed) that
	// ever owned the port, to tell a late return apart from an unknown port.
	HistoryByExtPort(ctx context.Context, runID string, proto model.Protocol, mappedPort uint16) (*model.Mapping, error)

	// InsertMapping persists a new active mapping. It must fail if the mapped
	// port already has an active owner.
	InsertMapping(ctx context.Context, runID string, m *model.Mapping) (id int64, err error)

	// UpdateMapping persists state/last-used/expiry changes.
	UpdateMapping(ctx context.Context, runID string, m *model.Mapping) error

	// CloseMapping marks an active mapping closed immediately and returns
	// whether a row was updated. Used for RST / completed FIN exchanges.
	CloseMapping(ctx context.Context, runID string, id int64, now time.Time) (bool, error)

	// CountActive returns the number of active mappings (after the sweep).
	CountActive(ctx context.Context, runID string, now time.Time) (int, error)

	// ListMappings returns mappings in id order; activeOnly hides closed rows.
	ListMappings(ctx context.Context, runID string, activeOnly bool) ([]*model.Mapping, error)

	// AppendEvent writes one decision-log row.
	AppendEvent(ctx context.Context, e *model.Event) error

	// ListEvents returns the decision log in seq/id order.
	ListEvents(ctx context.Context, runID string, limit int) ([]*model.Event, error)

	// SetClock persists the run clock high-water mark.
	SetClock(ctx context.Context, runID string, now time.Time) error
}
