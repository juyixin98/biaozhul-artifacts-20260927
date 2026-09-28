// Package store defines the persistence contract and provides two adapters:
// an in-memory implementation for deterministic tests and a SQLite-backed one
// for local operation.
package store

import (
	"context"
	"encoding/json"
	"sync"
	"time"

	"fieldapply/internal/merge"
	"fieldapply/internal/model"
)

// Snapshot is everything the merge needs for one resource at one revision.
type Snapshot struct {
	ID        string
	Revision  int64
	Live      json.RawMessage
	Schema    model.Schema
	Owners    model.Owners
	CreatedAt time.Time
	UpdatedAt time.Time
}

// Commit is one fully-merged apply ready to be durably recorded.
type Commit struct {
	Manager   string
	Reason    string
	RunID     string
	BaseRev   int64
	Live      json.RawMessage
	Applied   json.RawMessage
	Owners    model.Owners
	Changes   model.ChangeSet
	NewOwners []string
	Forced    bool
	At        time.Time
}

// HistoryEntry is one auditable generation.
type HistoryEntry struct {
	ResourceID string          `json:"resource_id"`
	Revision   int64           `json:"revision"`
	RunID      string          `json:"run_id"`
	Manager    string          `json:"manager"`
	Reason     string          `json:"reason"`
	Forced     bool            `json:"forced"`
	Changes    model.ChangeSet `json:"changes"`
	At         time.Time       `json:"at"`
}

// Store is the persistence contract shared by both adapters.
type Store interface {
	// Create inserts a new resource and records its manager's initial shares.
	Create(ctx context.Context, id string, live, applied json.RawMessage, manager string, schema model.Schema) (*Snapshot, error)
	// Snapshot returns the current revision of a resource.
	Snapshot(ctx context.Context, id string) (*Snapshot, error)
	// AppliedOf returns a manager's last applied config.
	AppliedOf(ctx context.Context, id, manager string) (json.RawMessage, bool, error)
	// Commit records a merged apply atomically: live value, ownership table,
	// per-manager applied config and the history entry advance together.
	Commit(ctx context.Context, id string, c Commit) (int64, error)
	// History lists generations newest-first, up to limit (<=0 => 50).
	History(ctx context.Context, id string, limit int) ([]HistoryEntry, error)
	Close() error
}

// -----------------------------------------------------------------------------
// In-memory adapter
// -----------------------------------------------------------------------------

type memResource struct {
	revision  int64
	live      json.RawMessage
	schema    model.Schema
	owners    model.Owners
	createdAt time.Time
	updatedAt time.Time
	applied   map[string]json.RawMessage
	history   []HistoryEntry
}

// Memory is the deterministic Store used by tests and examples.
type Memory struct {
	mu sync.Mutex
	rs map[string]*memResource
}

func NewMemory() *Memory { return &Memory{rs: map[string]*memResource{}} }

func (m *Memory) Create(_ context.Context, id string, live, applied json.RawMessage, manager string, schema model.Schema) (*Snapshot, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, exists := m.rs[id]; exists {
		return nil, &model.Error{Category: model.CatStateConflict, Code: "resource_exists",
			Message: "resource " + id + " already exists"}
	}
	now := time.Now().UTC()
	r := &memResource{
		revision:  1,
		live:      live,
		schema:    schema,
		owners:    model.Owners{},
		createdAt: now,
		updatedAt: now,
		applied:   map[string]json.RawMessage{},
	}
	if manager != "" {
		claimAll(live, schema, r.owners, manager)
		if applied != nil {
			r.applied[manager] = applied
		}
	}
	m.rs[id] = r
	return r.snapshot(id), nil
}

func (m *Memory) Snapshot(_ context.Context, id string) (*Snapshot, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	r, ok := m.rs[id]
	if !ok {
		return nil, &model.Error{Category: model.CatNotFound, Code: "resource_not_found",
			Message: "resource " + id + " does not exist"}
	}
	return r.snapshot(id), nil
}

func (m *Memory) AppliedOf(_ context.Context, id, manager string) (json.RawMessage, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	r, ok := m.rs[id]
	if !ok {
		return nil, false, &model.Error{Category: model.CatNotFound, Code: "resource_not_found",
			Message: "resource " + id + " does not exist"}
	}
	raw, ok := r.applied[manager]
	return raw, ok, nil
}

func (m *Memory) Commit(_ context.Context, id string, c Commit) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	r, ok := m.rs[id]
	if !ok {
		return 0, &model.Error{Category: model.CatNotFound, Code: "resource_not_found",
			Message: "resource " + id + " does not exist"}
	}
	if c.BaseRev != r.revision {
		return 0, &model.Error{Category: model.CatStateConflict, Code: "revision_stale",
			Message: "resource was modified concurrently; re-read and retry"}
	}
	r.revision++
	r.live = c.Live
	r.owners = c.Owners.Clone()
	r.updatedAt = c.At
	r.applied[c.Manager] = c.Applied
	r.history = append(r.history, HistoryEntry{
		ResourceID: id,
		Revision:   r.revision,
		RunID:      c.RunID,
		Manager:    c.Manager,
		Reason:     c.Reason,
		Forced:     c.Forced,
		Changes:    c.Changes,
		At:         c.At,
	})
	return r.revision, nil
}

func (m *Memory) History(_ context.Context, id string, limit int) ([]HistoryEntry, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	r, ok := m.rs[id]
	if !ok {
		return nil, &model.Error{Category: model.CatNotFound, Code: "resource_not_found",
			Message: "resource " + id + " does not exist"}
	}
	if limit <= 0 {
		limit = 50
	}
	out := make([]HistoryEntry, 0, limit)
	for i := len(r.history) - 1; i >= 0 && len(out) < limit; i-- {
		out = append(out, r.history[i])
	}
	return out, nil
}

func (m *Memory) Close() error { return nil }

func (r *memResource) snapshot(id string) *Snapshot {
	return &Snapshot{
		ID:        id,
		Revision:  r.revision,
		Live:      append(json.RawMessage(nil), r.live...),
		Schema:    r.schema,
		Owners:    r.owners.Clone(),
		CreatedAt: r.createdAt,
		UpdatedAt: r.updatedAt,
	}
}

// claimAll computes ownership of every schema-aware leaf in a created body.
func claimAll(body json.RawMessage, schema model.Schema, owners model.Owners, manager string) {
	v, err := model.DecodeValue(body)
	if err != nil {
		return
	}
	for ps := range merge.Leaves(v, &schema) {
		owners.Add(ps, manager)
	}
}
