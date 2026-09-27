// Package router is the stateful routing engine.
//
// It owns the CURRENT authoritative snapshot and nothing else: configuration
// parsing lives in internal/config, the ring math in internal/ring, durable
// history in internal/store and persistence of run decisions in
// internal/replay. The router only:
//
//   - holds an atomic pointer to the current immutable Snapshot,
//   - applies membership/weight/health changes by building a new ring and
//     bumping a monotonic version,
//   - routes flows against the snapshot and counts outcomes.
//
// Concurrency: readers take an atomic snapshot pointer (zero locks on the hot
// path). Mutating operations take a write mutex and validate an expected
// version (optimistic concurrency); a mismatch is STATE_CONFLICT/VERSION.
package router

import (
	"fmt"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"flowrouter/internal/apperr"
	"flowrouter/internal/config"
	"flowrouter/internal/flow"
	"flowrouter/internal/ring"
)

// MemberInfo is the runtime view of one declared next-hop.
type MemberInfo struct {
	ID         string `json:"id"`
	Address    string `json:"address"`
	Weight     int    `json:"weight"`
	Up         bool   `json:"up"`
	OnRing     bool   `json:"on_ring"` // weight > 0 and up
	VNodes     int    `json:"vnodes"`  // vnodes when on ring
	DownReason string `json:"down_reason,omitempty"`
}

// Snapshot is one immutable routing generation. A new pointer is published on
// every effective change; old snapshots stay readable by in-flight requests
// until they finish.
type Snapshot struct {
	Version     int64
	Fingerprint string // identity of the member/health input set
	ChangedAt   time.Time
	Change      string // human-readable reason this generation exists
	Ring        *ring.Ring
	Members     []MemberInfo // canonical order: ascending ID
	// Effective counts:
	NumMembers    int
	NumUp         int
	NumOnRing     int
	TotalWeight   int
	UpTotalWeight int
}

// Decision is one routing result, suitable for persistence/replay.
type Decision struct {
	Version  int64     `json:"version"`
	FlowKey  string    `json:"flow_key"`
	FlowHash uint64    `json:"flow_hash"`
	MemberID string    `json:"member_id"`
	Reason   string    `json:"reason"`
	At       time.Time `json:"at"`
}

type memberCounters struct {
	routed atomic.Int64
}

// Router is the engine; construct with New.
type Router struct {
	mu          sync.Mutex // serializes all mutating transitions
	snap        atomic.Pointer[Snapshot]
	vnodesPerW  int
	maxVNodes   int
	totalRouted atomic.Int64
	noRoute     atomic.Int64
	counters    sync.Map // memberID -> *memberCounters
}

// New constructs an empty router (version 0, no members). Use Load or
// ReplaceMembers to publish the first generation.
func New(vnodesPerWeight, maxVNodes int) *Router {
	r := &Router{vnodesPerW: vnodesPerWeight, maxVNodes: maxVNodes}
	r.snap.Store(&Snapshot{Version: 0, ChangedAt: time.Now(), Change: "init"})
	return r
}

// Current returns the active immutable snapshot (never nil).
func (r *Router) Current() *Snapshot { return r.snap.Load() }

func (r *Router) counterFor(id string) *memberCounters {
	v, _ := r.counters.LoadOrStore(id, &memberCounters{})
	return v.(*memberCounters)
}

// build is the single transition function: it derives the effective member
// set (positive weight and not marked down), builds the ring and publishes a
// new snapshot. It must be called with r.mu held.
//
// fingerprint identifies the exact inputs; identical inputs produce no new
// version (idempotent reload), returning changed=false.
func (r *Router) build(members []config.Member, down map[string]bool, downReason map[string]string,
	expected int64, change string) (*Snapshot, bool, error) {
	cur := r.snap.Load()
	if cur.Version != expected {
		return nil, false, apperr.Conflict("VERSION",
			fmt.Sprintf("expected version %d but current is %d", expected, cur.Version))
	}

	ids := make([]string, 0, len(members))
	byID := make(map[string]config.Member, len(members))
	totalW, upW, numUp := 0, 0, 0
	for _, m := range members {
		ids = append(ids, m.ID)
		byID[m.ID] = m
		totalW += m.Weight
		if !down[m.ID] {
			numUp++
			upW += m.Weight
		}
	}
	sort.Strings(ids)

	fp := fingerprint(ids, byID, down)
	if cur.Version > 0 && fp == cur.Fingerprint {
		return cur, false, nil // no effective change; version unchanged
	}

	next := r.construct(ids, byID, down, downReason, change, fp, cur.Version+1)
	if next == nil {
		return nil, false, apperr.Compute("RING_BUILD_FAILED", "ring construction failed")
	}
	r.snap.Store(next)
	return next, true, nil
}

// construct builds the immutable snapshot for the given inputs without version
// checks. Returns nil only when ring.Build fails structurally.
func (r *Router) construct(ids []string, byID map[string]config.Member,
	down map[string]bool, downReason map[string]string, change, fp string, version int64) *Snapshot {
	totalW, upW, numUp := 0, 0, 0
	eff := make([]ring.Member, 0, len(ids))
	infos := make([]MemberInfo, 0, len(ids))
	for _, id := range ids {
		m := byID[id]
		isDown := down[id]
		onRing := m.Weight > 0 && !isDown
		totalW += m.Weight
		if !isDown {
			numUp++
			upW += m.Weight
		}
		if onRing {
			eff = append(eff, ring.Member{ID: id, Weight: m.Weight})
		}
		infos = append(infos, MemberInfo{
			ID: id, Address: m.Address, Weight: m.Weight,
			Up: !isDown, OnRing: onRing,
			DownReason: downReason[id],
		})
		r.counterFor(id) // ensure counter exists
	}
	rg, err := ring.Build(eff, r.vnodesPerW, r.maxVNodes)
	if err != nil {
		return nil
	}
	for i := range infos {
		if c, ok := rg.VNodeCounts()[infos[i].ID]; ok {
			infos[i].VNodes = c
		}
	}
	return &Snapshot{
		Version: version, Fingerprint: fp,
		ChangedAt: time.Now(), Change: change, Ring: rg, Members: infos,
		NumMembers: len(ids), NumUp: numUp, NumOnRing: len(eff),
		TotalWeight: totalW, UpTotalWeight: upW,
	}
}

// Restore rehydrates router state at startup from the latest persisted ring
// version. It is only valid while the router is still at its initial version
// 0. The supplied version becomes the live version verbatim, so post-restart
// mutations continue the same monotonic sequence instead of overwriting rows.
func (r *Router) Restore(members []config.Member, down map[string]bool, downReason map[string]string,
	fp string, version int64) (*Snapshot, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.snap.Load().Version != 0 {
		return nil, apperr.Conflict("ALREADY_LOADED", "router already initialized")
	}
	if version < 1 {
		return nil, apperr.Invalid("BAD_RESTORE_VERSION", "restored version must be >= 1")
	}
	if err := validateMemberSet(members); err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(members))
	byID := make(map[string]config.Member, len(members))
	for _, m := range members {
		ids = append(ids, m.ID)
		byID[m.ID] = m
	}
	sort.Strings(ids)
	snap := r.construct(ids, byID, down, downReason, "restored", fp, version)
	if snap == nil {
		return nil, apperr.Compute("RING_BUILD_FAILED", "ring construction failed during restore")
	}
	r.snap.Store(snap)
	return snap, nil
}

// Load installs the initial configuration, requiring the router to be at
// version 0. Used at startup; reloads use ReplaceMembers with CAS.
func (r *Router) Load(members []config.Member, down map[string]bool) (*Snapshot, bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.snap.Load().Version != 0 {
		return nil, false, apperr.Conflict("ALREADY_LOADED",
			"router already initialized; use the reload endpoint with a version")
	}
	return r.build(members, down, map[string]string{}, 0, "initial_load")
}

// ReplaceMembers applies a full new member set (config reload). The persisted
// down-set is supplied by the caller (health state is runtime state kept
// across reloads for IDs that still exist).
func (r *Router) ReplaceMembers(expected int64, members []config.Member,
	down map[string]bool, downReason map[string]string) (*Snapshot, bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := validateMemberSet(members); err != nil {
		return nil, false, err
	}
	return r.build(members, down, downReason, expected, "replace_members")
}

// SetWeight changes one member's weight. Weight 0 removes it from the ring
// while retaining its identity.
func (r *Router) SetWeight(expected int64, id string, weight int) (*Snapshot, bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if weight < 0 {
		return nil, false, apperr.Invalid("NEGATIVE_WEIGHT", "weight must be >= 0")
	}
	members, down, reasons := r.currentInputs()
	if _, ok := findMember(members, id); !ok {
		return nil, false, apperr.Invalid("UNKNOWN_MEMBER", "no such member: "+id)
	}
	for i := range members {
		if members[i].ID == id {
			members[i].Weight = weight
		}
	}
	return r.build(members, down, reasons, expected, "set_weight:"+id)
}

// SetDown immediately excludes a member from the ring. Already-down members
// are a no-op (changed=false).
func (r *Router) SetDown(expected int64, id, reason string) (*Snapshot, bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	members, down, reasons := r.currentInputs()
	if _, ok := findMember(members, id); !ok {
		return nil, false, apperr.Invalid("UNKNOWN_MEMBER", "no such member: "+id)
	}
	if down[id] {
		return r.snap.Load(), false, nil
	}
	down[id] = true
	if reason == "" {
		reason = "manual"
	}
	reasons[id] = reason
	return r.build(members, down, reasons, expected, "member_down:"+id)
}

// SetUp re-admits a recovered member. Recovery does not silently restore the
// old mapping: a new ring generation is built and versioned reassignment
// runs (only the recovered member's new vnode arcs move).
func (r *Router) SetUp(expected int64, id string) (*Snapshot, bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	members, down, reasons := r.currentInputs()
	if _, ok := findMember(members, id); !ok {
		return nil, false, apperr.Invalid("UNKNOWN_MEMBER", "no such member: "+id)
	}
	if !down[id] {
		return r.snap.Load(), false, nil
	}
	delete(down, id)
	delete(reasons, id)
	return r.build(members, down, reasons, expected, "member_recovered:"+id)
}

func (r *Router) currentInputs() ([]config.Member, map[string]bool, map[string]string) {
	s := r.snap.Load()
	members := make([]config.Member, len(s.Members))
	down := map[string]bool{}
	reasons := map[string]string{}
	for i, mi := range s.Members {
		members[i] = config.Member{ID: mi.ID, Address: mi.Address, Weight: mi.Weight}
		if !mi.Up {
			down[mi.ID] = true
			reasons[mi.ID] = mi.DownReason
		}
	}
	return members, down, reasons
}

func validateMemberSet(members []config.Member) error {
	seen := map[string]bool{}
	for _, m := range members {
		if m.ID == "" {
			return apperr.Invalid("MEMBER_EMPTY_ID", "member id must not be empty")
		}
		if seen[m.ID] {
			return apperr.Invalid("MEMBER_DUPLICATE_ID", "duplicate member id: "+m.ID)
		}
		seen[m.ID] = true
		if m.Weight < 0 {
			return apperr.Invalid("MEMBER_NEGATIVE_WEIGHT", "negative weight for "+m.ID)
		}
	}
	return nil
}

func findMember(members []config.Member, id string) (config.Member, bool) {
	for _, m := range members {
		if m.ID == id {
			return m, true
		}
	}
	return config.Member{}, false
}

// Route resolves a flow against the current generation and counts the outcome.
func (r *Router) Route(f flow.FiveTuple) (Decision, error) {
	s := r.snap.Load()
	h := flowHashOf(f)
	d := Decision{Version: s.Version, FlowKey: f.CanonicalKey(), FlowHash: h, At: time.Now()}

	if s.Ring == nil || s.Ring.Empty() {
		r.noRoute.Add(1)
		switch {
		case s.NumMembers == 0:
			return d, apperr.NoHealthy("NO_MEMBERS", "no next-hop members configured")
		case s.TotalWeight == 0:
			return d, apperr.NoHealthy("ZERO_TOTAL_WEIGHT", "all members have weight 0")
		default:
			return d, apperr.NoHealthy("ALL_DOWN", "all members are currently down")
		}
	}
	owner, ok := s.Ring.Lookup(h)
	if !ok {
		r.noRoute.Add(1)
		return d, apperr.NoHealthy("NO_OWNER", "ring has no owner for the flow hash")
	}
	d.MemberID = owner
	d.Reason = "normal"
	r.totalRouted.Add(1)
	r.counterFor(owner).routed.Add(1)
	return d, nil
}

// Counts is an operational traffic counter snapshot (NOT a share): number of
// routed flows per member since process start, across all versions. Divide by
// TotalRouted only over a defined flow corpus to obtain an actual traffic
// share.
type Counts struct {
	TotalRouted int64            `json:"total_routed"`
	NoRoute     int64            `json:"no_route"`
	PerMember   map[string]int64 `json:"per_member"`
}

func (r *Router) Counts() Counts {
	c := Counts{PerMember: map[string]int64{}}
	c.TotalRouted = r.totalRouted.Load()
	c.NoRoute = r.noRoute.Load()
	s := r.snap.Load()
	for _, mi := range s.Members {
		if v, ok := r.counters.Load(mi.ID); ok {
			c.PerMember[mi.ID] = v.(*memberCounters).routed.Load()
		} else {
			c.PerMember[mi.ID] = 0
		}
	}
	return c
}

func flowHashOf(f flow.FiveTuple) uint64 {
	return f.Hash()
}
