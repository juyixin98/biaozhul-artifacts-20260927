package hashring

import (
	"math/big"
	"strconv"
	"sync"
	"sync/atomic"

	"flexhash/internal/fherr"
)

// Decision is the fully explained result of one flow lookup.
type Decision struct {
	Bucket        int
	Owner         string // structural bucket owner
	Chosen        string // member actually selected (== Owner when healthy)
	Address       string
	Failover      bool // true when the structural owner was unusable
	ConfigVersion int64
	HealthRev     int64
}

// state bundles a ring snapshot with the current health revision. Both are
// immutable; writers publish a new *state atomically.
type state struct {
	ring      *Ring
	healthRev int64
}

// Manager owns the current snapshot. Reads (Resolve/Snapshot) are lock-free
// and safe for unlimited concurrency; mutators (ApplyConfig/SetHealth)
// serialize internally so state transitions stay linearizable.
type Manager struct {
	cur         atomic.Pointer[state]
	bucketCount int
	writeMu     sync.Mutex
}

// NewManager creates a manager with a fixed bucket count. The bucket count is
// topology identity and never changes at runtime.
func NewManager(bucketCount int) *Manager {
	if bucketCount < 1 {
		bucketCount = 1
	}
	return &Manager{bucketCount: bucketCount}
}

// BucketCount returns the fixed bucket count.
func (m *Manager) BucketCount() int { return m.bucketCount }

// Bootstrap installs version 1 from the first persisted configuration.
// healthRev is the health revision restored from storage (0 on fresh start).
func (m *Manager) Bootstrap(version int64, members []Member, healthRev int64) (*Ring, []int, error) {
	const op = "hashring.Bootstrap"
	if cur := m.cur.Load(); cur != nil && cur.ring.Version != 0 {
		return nil, nil, fherr.New(fherr.KindStateConflict, op, "manager already bootstrapped")
	}
	if version < 1 {
		return nil, nil, fherr.New(fherr.KindInput, op, "initial version must be >= 1")
	}
	mm := memberMap(members)
	ring, moved, err := buildRing(version, m.bucketCount, mm, nil)
	if err != nil {
		return nil, nil, err
	}
	m.cur.Store(&state{ring: ring, healthRev: healthRev})
	return ring, moved, nil
}

// ApplyConfig transitions to a new config version. Health of surviving
// members is preserved; new members take the Healthy flag supplied here.
// Buckets move minimally; the returned moved slice lists exactly the buckets
// whose structural owner changed.
func (m *Manager) ApplyConfig(version int64, members []Member) (*Ring, []int, error) {
	const op = "hashring.ApplyConfig"
	m.writeMu.Lock()
	defer m.writeMu.Unlock()

	cur := m.cur.Load()
	if cur == nil || cur.ring.Version == 0 {
		return nil, nil, fherr.New(fherr.KindStateConflict, op, "manager not bootstrapped")
	}
	if version != cur.ring.Version+1 {
		return nil, nil, fherr.New(fherr.KindStateConflict, op,
			"non-contiguous version: have "+strconv.FormatInt(cur.ring.Version, 10)+
				", got "+strconv.FormatInt(version, 10))
	}

	desired := memberMap(members)
	// Preserve observed health for survivors.
	for id, dm := range desired {
		if old, ok := cur.ring.Members[id]; ok {
			dm.Healthy = old.Healthy
			desired[id] = dm
		}
	}
	ring, moved, err := buildRing(version, m.bucketCount, desired, cur.ring)
	if err != nil {
		return nil, nil, err
	}
	m.cur.Store(&state{ring: ring, healthRev: cur.healthRev})
	return ring, moved, nil
}

// RestoreConfig installs a ring while rebuilding from the event log. It
// accepts any increasing version and an explicit health revision.
func (m *Manager) RestoreConfig(version int64, members []Member, healthRev int64) error {
	const op = "hashring.RestoreConfig"
	m.writeMu.Lock()
	defer m.writeMu.Unlock()
	cur := m.cur.Load()
	if cur != nil && version <= cur.ring.Version {
		return fherr.New(fherr.KindStateConflict, op,
			"restore version not greater than current")
	}
	desired := memberMap(members)
	var old *Ring
	if cur != nil {
		old = cur.ring
	}
	ring, _, err := buildRing(version, m.bucketCount, desired, old)
	if err != nil {
		return err
	}
	m.cur.Store(&state{ring: ring, healthRev: healthRev})
	return nil
}

// SetHealth applies an observed health transition. Returns the new health
// revision. Marking a member whose state is unchanged is a state conflict
// (no-op transitions are not logged, so revisions stay meaningful).
func (m *Manager) SetHealth(id string, healthy bool) (int64, error) {
	const op = "hashring.SetHealth"
	m.writeMu.Lock()
	defer m.writeMu.Unlock()
	cur := m.cur.Load()
	if cur == nil || cur.ring.Version == 0 {
		return 0, fherr.New(fherr.KindStateConflict, op, "manager not bootstrapped")
	}
	target, ok := cur.ring.Members[id]
	if !ok {
		return 0, fherr.New(fherr.KindInput, op, "unknown member: "+id)
	}
	if target.Healthy == healthy {
		return 0, fherr.New(fherr.KindStateConflict, op,
			"member "+id+" already marked "+healthWord(healthy))
	}
	target.Healthy = healthy
	// Copy-on-write: owner slice is read-only and shared.
	members := make(map[string]Member, len(cur.ring.Members))
	for k, v := range cur.ring.Members {
		members[k] = v
	}
	members[id] = target
	ring := &Ring{
		Version:     cur.ring.Version,
		BucketCount: cur.ring.BucketCount,
		Members:     members,
		owner:       cur.ring.owner, // shared, immutable
	}
	newRev := cur.healthRev + 1
	m.cur.Store(&state{ring: ring, healthRev: newRev})
	return newRev, nil
}

// RestoreHealth replays a health event from the log.
func (m *Manager) RestoreHealth(id string, healthy bool, rev int64) error {
	const op = "hashring.RestoreHealth"
	m.writeMu.Lock()
	defer m.writeMu.Unlock()
	cur := m.cur.Load()
	if cur == nil {
		return fherr.New(fherr.KindStateConflict, op, "manager not bootstrapped")
	}
	if rev != cur.healthRev+1 {
		return fherr.New(fherr.KindStateConflict, op, "non-contiguous health revision")
	}
	target, ok := cur.ring.Members[id]
	if !ok {
		return fherr.New(fherr.KindInput, op, "unknown member: "+id)
	}
	target.Healthy = healthy
	members := make(map[string]Member, len(cur.ring.Members))
	for k, v := range cur.ring.Members {
		members[k] = v
	}
	members[id] = target
	ring := &Ring{
		Version:     cur.ring.Version,
		BucketCount: cur.ring.BucketCount,
		Members:     members,
		owner:       cur.ring.owner,
	}
	m.cur.Store(&state{ring: ring, healthRev: rev})
	return nil
}

// Snapshot returns the current ring and health revision.
func (m *Manager) Snapshot() (*Ring, int64, error) {
	cur := m.cur.Load()
	if cur == nil || cur.ring.Version == 0 {
		return nil, 0, fherr.New(fherr.KindStateConflict, "hashring.Snapshot",
			"routing table not initialized")
	}
	return cur.ring, cur.healthRev, nil
}

// Current returns the snapshot without error for callers that have just
// bootstrapped/replayed. It panics if the manager is uninitialized.
func (m *Manager) Current() (*Ring, int64) {
	cur := m.cur.Load()
	if cur == nil || cur.ring.Version == 0 {
		panic("hashring: Current called before bootstrap")
	}
	return cur.ring, cur.healthRev
}

// Resolve routes one canonical flow key against the current snapshot.
//
// Failover selection (weighted): among healthy members with positive weight,
// pick the one minimizing affinity(bucket, m)/weight(m), ties by member ID.
// Comparing fractions exactly with cross-multiplied big.Int products avoids
// floating-point platform differences: s1*w2 < s2*w1.
func (m *Manager) Resolve(flowKey string) (Decision, error) {
	ring, rev, err := m.Snapshot()
	if err != nil {
		return Decision{}, err
	}
	return ResolveOn(ring, rev, flowKey), nil
}

// ResolveOn is the pure decision function over an explicit snapshot so tests
// and the replay package can evaluate arbitrary rings. Returns a Decision
// with Chosen == "" and KindUnavailable when no healthy positive-weight
// member exists.
func ResolveOn(ring *Ring, healthRev int64, flowKey string) Decision {
	b := ring.BucketOf(flowKey)
	ownerID := ring.Owner(b)
	d := Decision{Bucket: b, Owner: ownerID, ConfigVersion: ring.Version, HealthRev: healthRev}

	if owner, ok := ring.Members[ownerID]; ok && owner.Healthy && owner.Weight > 0 {
		d.Chosen = owner.ID
		d.Address = owner.Address
		return d
	}

	// Failover path: scan all eligible members and keep the minimum
	// score/weight, ties by ID.
	var bestID, bestAddr string
	var bestScore *big.Int
	var bestWeight int64
	found := false
	for id, mm := range ring.Members {
		if !mm.Healthy || mm.Weight <= 0 {
			continue
		}
		s := new(big.Int).SetUint64(affinity(b, id))
		w := int64(mm.Weight)
		if !found {
			bestID, bestAddr, bestScore, bestWeight, found = id, mm.Address, s, w, true
			continue
		}
		if lessFrac(s, w, bestScore, bestWeight) ||
			(!lessFrac(bestScore, bestWeight, s, w) && id < bestID) {
			bestID, bestAddr, bestScore, bestWeight = id, mm.Address, s, w
		}
	}
	if !found {
		return d // Chosen empty: caller maps to unavailable
	}
	d.Failover = true
	d.Chosen = bestID
	d.Address = bestAddr
	return d
}

// lessFrac reports n1/d1 < n2/d2 exactly (denominators positive).
func lessFrac(n1 *big.Int, d1 int64, n2 *big.Int, d2 int64) bool {
	l := new(big.Int).Mul(n1, big.NewInt(d2))
	r := new(big.Int).Mul(n2, big.NewInt(d1))
	return l.Cmp(r) < 0
}

func memberMap(members []Member) map[string]Member {
	out := make(map[string]Member, len(members))
	for _, m := range members {
		out[m.ID] = m
	}
	return out
}

func healthWord(h bool) string {
	if h {
		return "healthy"
	}
	return "down"
}
