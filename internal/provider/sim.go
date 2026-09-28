package provider

import (
	"context"
	"fmt"
	"sync"
	"time"

	"infraplanner/internal/model"
	"infraplanner/internal/spec"
)

// Sim is the in-process simulated provider (adapter + fake cloud).
type Sim struct {
	mu     sync.Mutex // serializes mutating operations like a real control plane
	mem    *memory
	faults *faultBook
	idGen  func() string
}

// NewSim constructs an empty simulated provider.
func NewSim() *Sim {
	return &Sim{
		mem:    newMemory(),
		faults: newFaultBook(),
		idGen:  defaultIDGen,
	}
}

var idCounter struct {
	sync.Mutex
	n int
}

// defaultIDGen produces provider-looking opaque physical IDs. They are
// deliberately unrelated to logical names: tests can assert that a replace
// yields a new ID and that recovery never recreates a committed resource.
func defaultIDGen() string {
	idCounter.Lock()
	idCounter.n++
	n := idCounter.n
	idCounter.Unlock()
	return fmt.Sprintf("i-%08x%04x", time.Now().UnixNano()&0xffffffff, n)
}

// ---- administration (test harness / admin API) ----

// Seed installs a live resource directly into the simulated world.
func (s *Sim) Seed(l model.Live) {
	s.mem.mu.Lock()
	defer s.mem.mu.Unlock()
	if l.ID == "" {
		l.ID = s.idGen()
	}
	cp := l
	s.mem.byID[l.ID] = &cp
	s.mem.nameIndex[l.Key] = l.ID
}

// Reset empties the simulated world, capacity and armed faults.
func (s *Sim) Reset() {
	s.mem.mu.Lock()
	s.mem.byID = map[string]*model.Live{}
	s.mem.nameIndex = map[model.Key]string{}
	s.mem.seq = 0
	s.mem.capacity = map[model.Kind]int{}
	s.mem.mu.Unlock()
	s.faults.clear()
}

// SetCapacity sets the max live resources of a kind (0 = unlimited).
func (s *Sim) SetCapacity(k model.Kind, n int) {
	s.mem.mu.Lock()
	defer s.mem.mu.Unlock()
	s.mem.capacity[k] = n
}

// ArmFault arms an injected fault.
func (s *Sim) ArmFault(f Fault) { s.faults.arm(f) }

// Faults lists armed faults.
func (s *Sim) Faults() []Fault { return s.faults.list() }

// CountByKind returns live resource counts.
func (s *Sim) CountByKind() map[model.Kind]int {
	s.mem.mu.Lock()
	defer s.mem.mu.Unlock()
	out := map[model.Kind]int{}
	for _, l := range s.mem.byID {
		out[l.Key.Kind]++
	}
	return out
}

// ---- Provider implementation ----

// Observe returns the full simulated world.
func (s *Sim) Observe(ctx context.Context) (model.Observation, error) {
	if err := ctx.Err(); err != nil {
		return model.Observation{}, mapCtx(err)
	}
	s.mem.mu.Lock()
	defer s.mem.mu.Unlock()
	out := make([]model.Live, 0, len(s.mem.byID))
	for _, l := range s.mem.byID {
		out = append(out, copyLive(l))
	}
	return model.Observation{Resources: out, ObservedAt: time.Now().UTC()}, nil
}

// Create provisions a resource.
func (s *Sim) Create(ctx context.Context, d model.Desired, resolve RefResolver) (CreateResult, error) {
	if err := validateRefs(d, resolve); err != nil {
		return CreateResult{}, err
	}

	// A hang fault blocks until cancellation — models a kill mid-create.
	if f := s.faults.take(FaultCreateHang, d.Key()); f != nil {
		<-ctx.Done()
		return CreateResult{}, mapCtx(ctx.Err())
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	// Idempotency guard: if the logical name already exists, return the
	// existing resource instead of creating a second one. This is what makes
	// recovery after a lost response safe.
	if id, ok := s.mem.nameIndex[d.Key()]; ok {
		return CreateResult{ID: id, Live: copyLive(s.mem.byID[id])}, nil
	}

	if cap := s.mem.capacity[d.Kind]; cap > 0 {
		n := 0
		for _, l := range s.mem.byID {
			if l.Key.Kind == d.Kind {
				n++
			}
		}
		if n >= cap {
			return CreateResult{}, model.E(model.CatExhaustion, "capacity_full",
				"simulated capacity for %s reached (%d)", d.Kind, cap)
		}
		if f := s.faults.take(FaultCreateExhaust, d.Key()); f != nil {
			_ = f
			return CreateResult{}, model.E(model.CatExhaustion, "capacity_full",
				"injected exhaustion for %s", d.Key())
		}
	} else if f := s.faults.take(FaultCreateExhaust, d.Key()); f != nil {
		_ = f
		return CreateResult{}, model.E(model.CatExhaustion, "capacity_full",
			"injected exhaustion for %s", d.Key())
	}

	// Commit the resource into the world BEFORE deciding what the caller
	// sees, so a "lost response" leaves real state behind.
	id := s.idGen()
	live := model.Live{
		Key:       d.Key(),
		ID:        id,
		Attrs:     copyAttrs(d.Attrs),
		Refs:      copyRefs(d.Refs),
		Protected: d.Protected,
	}
	s.mem.byID[id] = &live
	s.mem.nameIndex[d.Key()] = id

	if f := s.faults.take(FaultCreateCommitLost, d.Key()); f != nil {
		_ = f
		// State is committed; caller is told the outcome is unknown.
		return CreateResult{}, model.E(model.CatCompute, "response_lost",
			"create of %s committed but response was lost", d.Key())
	}
	if f := s.faults.take(FaultCreateUnknown, d.Key()); f != nil {
		_ = f
		// For the genuinely-ambiguous case, roll the commit back so a
		// re-observe shows nothing until the caller retries.
		delete(s.mem.byID, id)
		delete(s.mem.nameIndex, d.Key())
		return CreateResult{}, model.E(model.CatCompute, "outcome_unknown",
			"create of %s returned an ambiguous result", d.Key())
	}

	return CreateResult{ID: id, Live: copyLive(&live)}, nil
}

// Update changes mutable fields in place; the physical ID is preserved.
func (s *Sim) Update(ctx context.Context, id string, d model.Desired, resolve RefResolver) (model.Live, error) {
	if err := validateRefs(d, resolve); err != nil {
		return model.Live{}, err
	}
	if f := s.faults.take(FaultUpdateHang, d.Key()); f != nil {
		_ = f
		<-ctx.Done()
		return model.Live{}, mapCtx(ctx.Err())
	}
	if f := s.faults.take(FaultUpdateTransient, d.Key()); f != nil {
		_ = f
		return model.Live{}, model.E(model.CatCompute, "transient",
			"injected transient update failure for %s", d.Key())
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	l, ok := s.mem.byID[id]
	if !ok {
		return model.Live{}, model.E(model.CatConflict, "gone",
			"resource %s (%s) vanished before update", d.Key(), id)
	}
	// Enforce immutability at the adapter too (defence in depth; the planner
	// should have produced a replace instead).
	ts := spec.Schema[d.Kind]
	for _, f := range ts.Fields {
		if f.Immutable {
			if lv := l.Attrs[f.Name]; lv != d.Attrs[f.Name] {
				return model.Live{}, model.E(model.CatConflict, "immutable_change",
					"attribute %q of %s is immutable", f.Name, d.Key())
			}
		}
	}
	l.Attrs = copyAttrs(d.Attrs)
	l.Refs = copyRefs(d.Refs)
	return copyLive(l), nil
}

// Delete removes a resource.
func (s *Sim) Delete(ctx context.Context, id string, k model.Key) error {
	if f := s.faults.take(FaultDeleteTransient, k); f != nil {
		_ = f
		return model.E(model.CatCompute, "transient",
			"injected transient delete failure for %s", k)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.mem.byID[id]; !ok {
		if s.faults.take(FaultDeleteNotFound, k) != nil {
			// consume semantics already; fall through to not-found result
		}
		// Delete is idempotent: an absent target is success.
		if cur, ok := s.mem.nameIndex[k]; ok && cur != id {
			return model.E(model.CatConflict, "id_reassigned",
				"logical %s now maps to %s, refusing stale delete of %s", k, cur, id)
		}
		return nil
	}
	delete(s.mem.byID, id)
	delete(s.mem.nameIndex, k)
	return nil
}

// ---- helpers ----

func validateRefs(d model.Desired, resolve RefResolver) error {
	ts := spec.Schema[d.Kind]
	for rn, wantKind := range ts.Refs {
		ref, ok := d.Refs[rn]
		if !ok {
			return model.E(model.CatInput, "missing_ref",
				"%s: internal error, reference %s missing", d.Key(), rn)
		}
		if ref.Kind != wantKind {
			return model.E(model.CatInput, "bad_ref_kind",
				"%s: reference %s wrong kind", d.Key(), rn)
		}
		id, err := resolve(ref)
		if err != nil {
			return model.E(model.CatConflict, "unresolved_ref",
				"%s: reference %s -> %s unresolved: %v", d.Key(), rn, ref.Key(), err)
		}
		if id == "" {
			return model.E(model.CatConflict, "unresolved_ref",
				"%s: reference %s -> %s has no physical id", d.Key(), rn, ref.Key())
		}
	}
	return nil
}

func mapCtx(err error) error {
	if err == nil {
		return nil
	}
	return model.E(model.CatCompute, "cancelled", "%v", err)
}

func copyLive(l *model.Live) model.Live {
	return model.Live{
		Key:       l.Key,
		ID:        l.ID,
		Attrs:     copyAttrs(l.Attrs),
		Refs:      copyRefs(l.Refs),
		Protected: l.Protected,
	}
}

func copyAttrs(a map[string]string) map[string]string {
	if len(a) == 0 {
		return map[string]string{}
	}
	m := make(map[string]string, len(a))
	for k, v := range a {
		m[k] = v
	}
	return m
}

func copyRefs(r map[string]model.Ref) map[string]model.Ref {
	if len(r) == 0 {
		return map[string]model.Ref{}
	}
	m := make(map[string]model.Ref, len(r))
	for k, v := range r {
		m[k] = v
	}
	return m
}
