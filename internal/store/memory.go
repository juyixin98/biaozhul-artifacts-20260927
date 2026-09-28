package store

import (
	"context"
	"sort"
	"sync"
	"time"

	"ipfragreasm/internal/netmodel"
)

// MemoryStore 是单进程、零外部依赖的 Store 实现，供测试与无 DSN 场景使用。
type MemoryStore struct {
	mu     sync.Mutex
	groups map[string]memGroup
	keyOf  map[netmodel.FragKey]string
}

type memGroup struct {
	record    GroupRecord
	fragments []FragmentRecord
}

// NewMemory 创建内存存储。
func NewMemory() *MemoryStore {
	return &MemoryStore{
		groups: make(map[string]memGroup),
		keyOf:  make(map[netmodel.FragKey]string),
	}
}

func (m *MemoryStore) UpsertGroup(_ context.Context, g GroupRecord) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	id := g.Key.String()
	existing, ok := m.groups[id]
	if !ok {
		m.groups[id] = memGroup{record: g}
	} else {
		existing.record = g
		m.groups[id] = existing
	}
	m.keyOf[g.Key] = id
	return nil
}

func (m *MemoryStore) GetGroup(_ context.Context, key netmodel.FragKey) (GroupRecord, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	g, ok := m.groups[key.String()]
	if !ok {
		return GroupRecord{}, false, nil
	}
	return g.record, true, nil
}

func (m *MemoryStore) DeleteGroup(_ context.Context, key netmodel.FragKey) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.groups, key.String())
	delete(m.keyOf, key)
	return nil
}

func (m *MemoryStore) AddFragment(_ context.Context, f FragmentRecord) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	g := m.groups[f.Key.String()]
	g.fragments = append(g.fragments, f)
	m.groups[f.Key.String()] = g
	return nil
}

func (m *MemoryStore) ListFragments(_ context.Context, key netmodel.FragKey) ([]FragmentRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	g := m.groups[key.String()]
	out := make([]FragmentRecord, len(g.fragments))
	copy(out, g.fragments)
	sort.SliceStable(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out, nil
}

func (m *MemoryStore) DeleteFragments(_ context.Context, key netmodel.FragKey) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	g := m.groups[key.String()]
	g.fragments = nil
	m.groups[key.String()] = g
	return nil
}

func (m *MemoryStore) ListOpenGroups(_ context.Context) ([]GroupRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []GroupRecord
	for _, g := range m.groups {
		if !IsTerminalState(g.record.State) {
			out = append(out, g.record)
		}
	}
	return out, nil
}

func (m *MemoryStore) ListTerminalExpired(_ context.Context, now time.Time) ([]GroupRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []GroupRecord
	for _, g := range m.groups {
		r := g.record
		if IsTerminalState(r.State) && !r.ExpiresAt.IsZero() && !r.ExpiresAt.After(now) {
			out = append(out, r)
		}
	}
	return out, nil
}

func (m *MemoryStore) ListAllGroups(_ context.Context) ([]GroupRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]GroupRecord, 0, len(m.groups))
	for _, g := range m.groups {
		out = append(out, g.record)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key.String() < out[j].Key.String() })
	return out, nil
}

func (m *MemoryStore) CountFragments(_ context.Context) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	n := 0
	for _, g := range m.groups {
		n += len(g.fragments)
	}
	return n, nil
}

// Close 对内存存储为空操作。
func (m *MemoryStore) Close() error { return nil }
