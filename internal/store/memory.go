package store

import (
	"context"
	"sort"
	"sync"
	"time"
)

// MemoryStore is an in-process implementation of Store with the same
// atomicity guarantees: Apply advances the version and records the change
// under one lock, decisions are append-only, and historical state is derived
// from the immutable change history.
type MemoryStore struct {
	mu       sync.Mutex
	version  int64
	subs     map[string]Subscription
	changes  []SubscriptionVersion // strictly increasing Version order
	versions []RoutingVersion
	decision map[string]Decision
	order    []string // message-id insertion order
}

// NewMemoryStore creates an empty store at version 0 (no routing entries).
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{
		subs:     make(map[string]Subscription),
		decision: make(map[string]Decision),
	}
}

// Apply implements Store.
func (m *MemoryStore) Apply(ctx context.Context, change SubscriptionVersion, expectedVersion int64) (ApplyResult, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	cur, exists := m.subs[change.SubscriptionID]
	current := int64(-1)
	if exists {
		current = cur.Version
	}
	switch expectedVersion {
	case -2: // unconditional
	case -1:
		if exists && change.Kind == KindUpsert {
			return ApplyResult{
				Version: m.version, SubscriptionID: change.SubscriptionID,
				Conflict: true, CurrentVersion: current,
			}, nil
		}
	default:
		if expectedVersion != current {
			return ApplyResult{
				Version: m.version, SubscriptionID: change.SubscriptionID,
				Conflict: true, CurrentVersion: current,
			}, nil
		}
	}

	m.version++
	now := time.Now().UTC()
	change.Version = m.version
	change.CreatedAt = now

	switch change.Kind {
	case KindUpsert:
		m.subs[change.SubscriptionID] = Subscription{
			ID:           change.SubscriptionID,
			SubscriberID: change.SubscriberID,
			Filter:       change.Filter,
			UpdatedAt:    now,
			Version:      m.version,
		}
	case KindDelete:
		delete(m.subs, change.SubscriptionID)
	}

	m.changes = append(m.changes, change)
	m.versions = append(m.versions, RoutingVersion{
		Version: m.version, CreatedAt: now,
		SubscriptionID: change.SubscriptionID, Change: change.Kind,
	})
	return ApplyResult{
		Version: m.version, SubscriptionID: change.SubscriptionID,
		Change: change.Kind, CurrentVersion: m.version,
	}, nil
}

// CurrentVersion implements Store.
func (m *MemoryStore) CurrentVersion(ctx context.Context) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.version, nil
}

// GetSubscription implements Store.
func (m *MemoryStore) GetSubscription(ctx context.Context, id string) (Subscription, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if s, ok := m.subs[id]; ok {
		return s, nil
	}
	return Subscription{}, &NotFoundError{Resource: "subscription", Key: id}
}

// ListSubscriptions implements Store.
func (m *MemoryStore) ListSubscriptions(ctx context.Context) ([]Subscription, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]Subscription, 0, len(m.subs))
	for _, s := range m.subs {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}

// SnapshotAt implements Store by replaying immutable history. As-of-v state is
// derived from the change log, not from current state, so it stays correct
// after deletes and later updates.
func (m *MemoryStore) SnapshotAt(ctx context.Context, v int64, fromVersion int64) (SnapshotData, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if v > m.version {
		v = m.version
	}
	state := make(map[string]Subscription)
	var delta []SubscriptionVersion
	for _, c := range m.changes {
		if c.Version > v {
			break
		}
		if c.Version > fromVersion {
			delta = append(delta, c)
		}
		switch c.Kind {
		case KindUpsert:
			state[c.SubscriptionID] = Subscription{
				ID:           c.SubscriptionID,
				SubscriberID: c.SubscriberID,
				Filter:       c.Filter,
				UpdatedAt:    c.CreatedAt,
				Version:      c.Version,
			}
		case KindDelete:
			delete(state, c.SubscriptionID)
		}
	}
	subs := make([]Subscription, 0, len(state))
	for _, s := range state {
		subs = append(subs, s)
	}
	sort.Slice(subs, func(i, j int) bool { return subs[i].ID < subs[j].ID })
	return SnapshotData{Version: v, Subs: subs, Changes: delta}, nil
}

// SaveDecision implements Store.
func (m *MemoryStore) SaveDecision(ctx context.Context, d Decision) (Decision, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if existing, ok := m.decision[d.MessageID]; ok {
		return existing, false, nil
	}
	m.decision[d.MessageID] = d
	m.order = append(m.order, d.MessageID)
	return d, true, nil
}

// GetDecision implements Store.
func (m *MemoryStore) GetDecision(ctx context.Context, messageID string) (Decision, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if d, ok := m.decision[messageID]; ok {
		return d, nil
	}
	return Decision{}, &NotFoundError{Resource: "decision", Key: messageID}
}

// ListDecisions implements Store, newest first, cursor = message id of the
// oldest row returned by the previous page.
func (m *MemoryStore) ListDecisions(ctx context.Context, limit int, cursor string) (DecisionPage, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	start := len(m.order)
	if cursor != "" {
		found := false
		for i := len(m.order) - 1; i >= 0; i-- {
			if m.order[i] == cursor {
				start = i
				found = true
				break
			}
		}
		if !found {
			return DecisionPage{}, &NotFoundError{Resource: "cursor", Key: cursor}
		}
	}
	page := DecisionPage{}
	out := make([]Decision, 0, limit)
	for i := start - 1; i >= 0 && len(out) < limit; i-- {
		out = append(out, m.decision[m.order[i]])
	}
	page.Decisions = out
	if start-len(out) > 0 {
		page.HasMore = true
		page.NextCursor = out[len(out)-1].MessageID
	}
	return page, nil
}
