package storage

import (
	"context"
	"encoding/json"
	"sync"

	"example.com/cgcoord/protocol"
)

// MemoryStore is an in-process Store. Transactions are serialized by a
// single mutex, which makes the isolation level effectively SERIALIZABLE.
// State blobs are deep-copied through JSON so callers cannot mutate stored
// state by retaining pointers.
type MemoryStore struct {
	mu     sync.Mutex
	groups map[string]*memoryGroup
}

type memoryGroup struct {
	state  []byte // JSON state blob, nil until first save
	events []protocol.Event
}

// NewMemoryStore creates an empty memory store.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{groups: make(map[string]*memoryGroup)}
}

// Close implements Store.
func (s *MemoryStore) Close() error { return nil }

// ListGroups implements Store.
func (s *MemoryStore) ListGroups(ctx context.Context) ([]string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	names := make([]string, 0, len(s.groups))
	for name := range s.groups {
		names = append(names, name)
	}
	return names, nil
}

// Begin implements Store.
func (s *MemoryStore) Begin(ctx context.Context) (Tx, error) {
	s.mu.Lock()
	snap := make(map[string]*memoryGroup, len(s.groups))
	for name, g := range s.groups {
		cp := &memoryGroup{state: append([]byte(nil), g.state...)}
		for _, e := range g.events {
			data, err := json.Marshal(e)
			if err != nil {
				s.mu.Unlock()
				return nil, err
			}
			de, err := protocol.UnmarshalEvent(data)
			if err != nil {
				s.mu.Unlock()
				return nil, err
			}
			cp.events = append(cp.events, de)
		}
		snap[name] = cp
	}
	return &memoryTx{store: s, snap: snap}, nil
}

type memoryTx struct {
	store    *MemoryStore
	snap     map[string]*memoryGroup
	done     bool
	created  map[string]bool
}

func (t *memoryTx) group(name string) (*memoryGroup, error) {
	g, ok := t.snap[name]
	if !ok {
		return nil, ErrNotFound
	}
	return g, nil
}

func (t *memoryTx) CreateGroup(name string) error {
	if _, exists := t.snap[name]; exists {
		return ErrGroupExists
	}
	if t.created == nil {
		t.created = map[string]bool{}
	}
	t.snap[name] = &memoryGroup{events: []protocol.Event{}}
	t.created[name] = true
	return nil
}

func (t *memoryTx) SaveGroupState(state *protocol.GroupState) error {
	g, err := t.group(state.Name)
	if err != nil {
		return err
	}
	raw, err := json.Marshal(state)
	if err != nil {
		return err
	}
	g.state = raw
	return nil
}

func (t *memoryTx) AppendEvent(e protocol.Event) (protocol.Seq, error) {
	g, err := t.group(e.Group)
	if err != nil {
		return 0, err
	}
	e.Seq = protocol.Seq(len(g.events))
	// Round-trip through the wire codec so stored events carry the same
	// registered detail types the SQL backend returns.
	data, err := protocol.MarshalEvent(e)
	if err != nil {
		return 0, err
	}
	decoded, err := protocol.UnmarshalEvent(data)
	if err != nil {
		return 0, err
	}
	g.events = append(g.events, decoded)
	return e.Seq, nil
}

func (t *memoryTx) LoadGroup(name string) (*protocol.GroupState, error) {
	g, err := t.group(name)
	if err != nil {
		return nil, err
	}
	if g.state == nil {
		return nil, nil
	}
	var s protocol.GroupState
	if err := json.Unmarshal(g.state, &s); err != nil {
		return nil, err
	}
	return &s, nil
}

func (t *memoryTx) ReadEvents(group string, from protocol.Seq, limit int) ([]protocol.Event, error) {
	g, err := t.group(group)
	if err != nil {
		return nil, err
	}
	if int(from) >= len(g.events) {
		return []protocol.Event{}, nil
	}
	chunk := g.events[int(from):]
	if len(chunk) > limit {
		chunk = chunk[:limit]
	}
	out := make([]protocol.Event, 0, len(chunk))
	for _, raw := range chunk {
		// Round-trip via the wire codec to return deep copies with the same
		// registered detail types the SQL backend yields.
		data, err := json.Marshal(raw)
		if err != nil {
			return nil, err
		}
		e, err := protocol.UnmarshalEvent(data)
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, nil
}

func (t *memoryTx) Commit() error {
	if t.done {
		return nil
	}
	t.done = true
	t.store.groups = t.snap
	t.store.mu.Unlock()
	return nil
}

func (t *memoryTx) Rollback() {
	if t.done {
		return
	}
	t.done = true
	t.store.mu.Unlock()
}
