// Package engine implements the IGMPv2 querier-side membership and query
// timing state machine. It maintains, per (interface, group), the member
// set, the group expiry timer, the current query round (generation), and
// the last-member query phase.
//
// Scope: this is a timing/membership model for offline replay. It is NOT
// a multicast routing protocol implementation: there are no packets, no
// PIM/DR election, no IGMPv3 source lists.
//
// Semantics (all times in milliseconds, driven by the injected clock):
//
//   - report: refreshes the group timer to now + GroupMembershipInterval.
//     A report carrying an explicit generation older than the group's
//     current round is stale: it is recorded but changes nothing, so a
//     late answer to an old query round can never overwrite a newer round.
//   - leave: removes that member. If other members remain, nothing else
//     happens (a single leave never deletes a multi-member group). If the
//     last member leaves, the group enters the last-member phase with
//     deadline now + LastMemberQueryTime.
//   - report during the last-member phase: rescues the group; the timer is
//     reset to the full GroupMembershipInterval.
//   - general/group query: bumps the interface query round (generation)
//     and the current round of the affected groups.
//   - suppressed_report: recorded for audit only; never touches timers.
//   - group timer expiry: the group is deleted and its forwarding-table
//     interval is closed. Expiry is inclusive: a group whose deadline is
//     T is gone at time T.
package engine

import (
	"fmt"
	"net/netip"
	"sort"

	"igmpq/internal/cats"
	"igmpq/internal/config"
	"igmpq/internal/model"
	"igmpq/internal/simclock"
)

// Transition types recorded in the audit trail.
const (
	TGroupCreated           = "group_created"
	TReportAccepted         = "report_accepted"
	TReportSuppressed       = "report_suppressed"
	TStaleReportIgnored     = "stale_report_ignored"
	TMemberLeft             = "member_left"
	TLastMemberQueryStarted = "last_member_query_started"
	TQueryIssued            = "query_issued"
	TGroupExpired           = "group_expired"
)

// Reasons attached to transitions (machine-comparable).
const (
	ReasonMembersRemaining   = "members_remaining"
	ReasonRescuedLastMember  = "rescued_last_member"
	ReasonMembershipTimeout  = "membership_timeout"
	ReasonLastMemberTimeout  = "last_member_timeout"
	ReasonOlderThanCurrRound = "older_than_current_round"
)

// Transition is one audited state change.
type Transition struct {
	Seq    int     `json:"seq"`
	TimeMS int64   `json:"time_ms"`
	Type   string  `json:"type"`
	Iface  string  `json:"iface,omitempty"`
	Group  string  `json:"group,omitempty"`
	Member string  `json:"member,omitempty"`
	Gen    *uint64 `json:"gen"`
	Reason string  `json:"reason,omitempty"`
	Detail string  `json:"detail,omitempty"`
}

// Rejection is a refused event, with the stable failure category.
type Rejection struct {
	EventIndex int    `json:"event_index"`
	TimeMS     int64  `json:"time_ms"`
	Category   string `json:"category"`
	Iface      string `json:"iface,omitempty"`
	Group      string `json:"group,omitempty"`
	Member     string `json:"member,omitempty"`
	Reason     string `json:"reason"`
}

// Interval is a forwarding-table retention interval [StartMS, EndMS).
// EndMS is nil while the group is still present.
type Interval struct {
	StartMS int64  `json:"start_ms"`
	EndMS   *int64 `json:"end_ms"`
}

// GroupView is a snapshot of one live (interface, group) state.
type GroupView struct {
	Members         []string `json:"members"`
	ExpiryMS        int64    `json:"expiry_ms"`
	Round           uint64   `json:"round"`
	LastMember      bool     `json:"last_member"`
	SuppressedTotal int      `json:"suppressed_total"`
}

// Key identifies one (interface, group) forwarding entry.
type Key struct {
	Iface string
	Group string
}

// String renders the key as "iface/group".
func (k Key) String() string { return k.Iface + "/" + k.Group }

type groupState struct {
	members    map[string]int64 // member -> last report time
	expiry     int64
	round      uint64
	lastMember bool
	suppressed int
	since      int64 // start of the current forwarding interval
}

// Engine is the membership state machine. Not safe for concurrent use.
type Engine struct {
	cfg     config.Config
	derived config.Derived
	clk     *simclock.Manual

	ifaceGen map[string]uint64
	groups   map[Key]*groupState

	transitions []Transition
	rejections  []Rejection
	intervals   map[string][]Interval
	eventIdx    int
}

// New builds an engine. clk is the injected clock; the engine only ever
// reads time from it.
func New(cfg config.Config, clk *simclock.Manual) *Engine {
	ifaceGen := make(map[string]uint64, len(cfg.Interfaces))
	for _, ifc := range cfg.Interfaces {
		ifaceGen[ifc] = 0
	}
	return &Engine{
		cfg:       cfg,
		derived:   cfg.Derived(),
		clk:       clk,
		ifaceGen:  ifaceGen,
		groups:    map[Key]*groupState{},
		intervals: map[string][]Interval{},
	}
}

// Derived exposes the computed timing values.
func (e *Engine) Derived() config.Derived { return e.derived }

// Apply validates and applies one event, first firing any timers that
// expire at or before the event time. Malformed events are recorded as
// rejections (never silently dropped, never fatal to the replay).
func (e *Engine) Apply(ev model.Event) {
	idx := e.eventIdx
	e.eventIdx++

	if ev.TimeMS < 0 {
		e.reject(idx, ev, cats.BadEvent, fmt.Sprintf("negative time_ms %d", ev.TimeMS))
		return
	}
	if ev.TimeMS < e.clk.Now() {
		e.reject(idx, ev, cats.OutOfOrder,
			fmt.Sprintf("event time %d is before current replay time %d", ev.TimeMS, e.clk.Now()))
		return
	}
	e.advanceTo(ev.TimeMS)

	if !model.Types[ev.Type] {
		e.reject(idx, ev, cats.BadEvent, "unknown event type "+ev.Type)
		return
	}

	switch ev.Type {
	case model.EventReport:
		e.onReport(idx, ev)
	case model.EventLeave:
		e.onLeave(idx, ev)
	case model.EventGeneralQuery:
		e.onGeneralQuery(idx, ev)
	case model.EventGroupQuery:
		e.onGroupQuery(idx, ev)
	case model.EventSuppressedReport:
		e.onSuppressedReport(idx, ev)
	}
}

// RunUntil advances replay time to t, firing every timer that expires at
// or before t. Use it after the last event to let pending expiries happen.
func (e *Engine) RunUntil(t int64) {
	e.advanceTo(t)
}

// Transitions returns the audit trail so far.
func (e *Engine) Transitions() []Transition { return e.transitions }

// Rejections returns the refused events so far.
func (e *Engine) Rejections() []Rejection { return e.rejections }

// Intervals returns forwarding-table retention intervals keyed by
// "iface/group".
func (e *Engine) Intervals() map[string][]Interval { return e.intervals }

// Snapshot returns the live state of one (iface, group), if present.
func (e *Engine) Snapshot(iface, group string) (GroupView, bool) {
	g, ok := e.groups[Key{Iface: iface, Group: group}]
	if !ok {
		return GroupView{}, false
	}
	return viewOf(g), true
}

// FinalGroups returns snapshots of all groups still present.
func (e *Engine) FinalGroups() map[string]GroupView {
	out := make(map[string]GroupView, len(e.groups))
	for k, g := range e.groups {
		out[k.String()] = viewOf(g)
	}
	return out
}

func viewOf(g *groupState) GroupView {
	members := make([]string, 0, len(g.members))
	for m := range g.members {
		members = append(members, m)
	}
	sort.Strings(members)
	return GroupView{
		Members:         members,
		ExpiryMS:        g.expiry,
		Round:           g.round,
		LastMember:      g.lastMember,
		SuppressedTotal: g.suppressed,
	}
}

// advanceTo moves time forward, expiring groups whose deadline passes.
// Expiry is inclusive: a group with expiry == t is removed at t.
func (e *Engine) advanceTo(t int64) {
	for {
		var minKey Key
		var minG *groupState
		found := false
		for k, g := range e.groups {
			if !found || g.expiry < minG.expiry || (g.expiry == minG.expiry && k.String() < minKey.String()) {
				minKey, minG, found = k, g, true
			}
		}
		if !found || minG.expiry > t {
			break
		}
		e.clk.AdvanceTo(minG.expiry)
		reason := ReasonMembershipTimeout
		if minG.lastMember {
			reason = ReasonLastMemberTimeout
		}
		e.closeInterval(minKey, minG.expiry)
		delete(e.groups, minKey)
		e.add(Transition{
			TimeMS: minG.expiry, Type: TGroupExpired,
			Iface: minKey.Iface, Group: minKey.Group,
			Reason: reason,
			Detail: fmt.Sprintf("reason=%s", reason),
		})
	}
	e.clk.AdvanceTo(t)
}

func (e *Engine) onReport(idx int, ev model.Event) {
	if !e.checkIface(idx, ev) || !e.checkGroup(idx, ev) || !e.checkMember(idx, ev) {
		return
	}
	gen := e.ifaceGen[ev.Iface]
	if ev.Gen != nil {
		if *ev.Gen > gen {
			e.reject(idx, ev, cats.FutureGeneration,
				fmt.Sprintf("report answers round %d but only %d query rounds issued on %s", *ev.Gen, gen, ev.Iface))
			return
		}
		gen = *ev.Gen
	}
	key := Key{Iface: ev.Iface, Group: ev.Group}
	now := e.clk.Now()
	g, ok := e.groups[key]
	if !ok {
		// A stale report must not resurrect an expired group.
		if ev.Gen != nil && *ev.Gen < e.ifaceGen[ev.Iface] {
			e.add(Transition{
				TimeMS: now, Type: TStaleReportIgnored,
				Iface: ev.Iface, Group: ev.Group, Member: ev.Member, Gen: ev.Gen,
				Reason: ReasonOlderThanCurrRound,
				Detail: fmt.Sprintf("report_gen=%d current_round=%d group_absent", *ev.Gen, e.ifaceGen[ev.Iface]),
			})
			return
		}
		g = &groupState{
			members: map[string]int64{ev.Member: now},
			expiry:  now + e.derived.GroupMembershipIntervalMS,
			round:   e.ifaceGen[ev.Iface],
			since:   now,
		}
		e.groups[key] = g
		e.openInterval(key, now)
		e.add(Transition{
			TimeMS: now, Type: TGroupCreated,
			Iface: ev.Iface, Group: ev.Group, Member: ev.Member, Gen: ev.Gen,
			Detail: fmt.Sprintf("members=1 expiry_ms=%d round=%d", g.expiry, g.round),
		})
		return
	}
	if ev.Gen != nil && *ev.Gen < g.round {
		e.add(Transition{
			TimeMS: now, Type: TStaleReportIgnored,
			Iface: ev.Iface, Group: ev.Group, Member: ev.Member, Gen: ev.Gen,
			Reason: ReasonOlderThanCurrRound,
			Detail: fmt.Sprintf("report_gen=%d current_round=%d", *ev.Gen, g.round),
		})
		return
	}
	g.members[ev.Member] = now
	rescued := g.lastMember
	g.lastMember = false
	g.expiry = now + e.derived.GroupMembershipIntervalMS
	reason := ""
	if rescued {
		reason = ReasonRescuedLastMember
	}
	e.add(Transition{
		TimeMS: now, Type: TReportAccepted,
		Iface: ev.Iface, Group: ev.Group, Member: ev.Member, Gen: ev.Gen,
		Reason: reason,
		Detail: fmt.Sprintf("members=%d expiry_ms=%d round=%d", len(g.members), g.expiry, g.round),
	})
}

func (e *Engine) onLeave(idx int, ev model.Event) {
	if !e.checkIface(idx, ev) || !e.checkGroup(idx, ev) || !e.checkMember(idx, ev) {
		return
	}
	key := Key{Iface: ev.Iface, Group: ev.Group}
	now := e.clk.Now()
	g, ok := e.groups[key]
	if !ok {
		e.reject(idx, ev, cats.UnknownMember,
			fmt.Sprintf("leave from %s but group %s has no state on %s", ev.Member, ev.Group, ev.Iface))
		return
	}
	if _, ok := g.members[ev.Member]; !ok {
		e.reject(idx, ev, cats.UnknownMember,
			fmt.Sprintf("leave from %s who is not a member of %s on %s", ev.Member, ev.Group, ev.Iface))
		return
	}
	delete(g.members, ev.Member)
	if len(g.members) > 0 {
		// Other members remain: a single leave must not delete the group.
		e.add(Transition{
			TimeMS: now, Type: TMemberLeft,
			Iface: ev.Iface, Group: ev.Group, Member: ev.Member,
			Reason: ReasonMembersRemaining,
			Detail: fmt.Sprintf("members_remaining=%d expiry_ms=%d", len(g.members), g.expiry),
		})
		return
	}
	// Last member left: enter the last-member phase. The querier would
	// send LMQC group-specific queries LMQI apart; we model the aggregate
	// deadline now + LMQI*LMQC.
	g.lastMember = true
	g.expiry = now + e.derived.LastMemberQueryTimeMS
	e.add(Transition{
		TimeMS: now, Type: TLastMemberQueryStarted,
		Iface: ev.Iface, Group: ev.Group, Member: ev.Member,
		Detail: fmt.Sprintf("deadline_ms=%d lmqt_ms=%d", g.expiry, e.derived.LastMemberQueryTimeMS),
	})
}

func (e *Engine) onGeneralQuery(idx int, ev model.Event) {
	if !e.checkIface(idx, ev) {
		return
	}
	e.ifaceGen[ev.Iface]++
	gen := e.ifaceGen[ev.Iface]
	n := 0
	for k, g := range e.groups {
		if k.Iface == ev.Iface {
			g.round = gen
			n++
		}
	}
	e.add(Transition{
		TimeMS: e.clk.Now(), Type: TQueryIssued, Iface: ev.Iface, Gen: &gen,
		Detail: fmt.Sprintf("scope=general groups=%d", n),
	})
}

func (e *Engine) onGroupQuery(idx int, ev model.Event) {
	if !e.checkIface(idx, ev) || !e.checkGroup(idx, ev) {
		return
	}
	key := Key{Iface: ev.Iface, Group: ev.Group}
	g, ok := e.groups[key]
	if !ok {
		e.reject(idx, ev, cats.UnknownGroup,
			fmt.Sprintf("group-specific query for %s which has no state on %s", ev.Group, ev.Iface))
		return
	}
	e.ifaceGen[ev.Iface]++
	gen := e.ifaceGen[ev.Iface]
	g.round = gen
	e.add(Transition{
		TimeMS: e.clk.Now(), Type: TQueryIssued, Iface: ev.Iface, Group: ev.Group, Gen: &gen,
		Detail: "scope=group",
	})
}

func (e *Engine) onSuppressedReport(idx int, ev model.Event) {
	if !e.checkIface(idx, ev) || !e.checkGroup(idx, ev) || !e.checkMember(idx, ev) {
		return
	}
	key := Key{Iface: ev.Iface, Group: ev.Group}
	g, ok := e.groups[key]
	if !ok {
		e.reject(idx, ev, cats.UnknownGroup,
			fmt.Sprintf("suppressed report for %s which has no state on %s", ev.Group, ev.Iface))
		return
	}
	// Host-side suppression (RFC 2236 §3): the querier observes nothing.
	// Record for audit; never touch membership or timers.
	g.suppressed++
	round := g.round
	e.add(Transition{
		TimeMS: e.clk.Now(), Type: TReportSuppressed,
		Iface: ev.Iface, Group: ev.Group, Member: ev.Member, Gen: &round,
		Detail: fmt.Sprintf("round=%d suppressed_total=%d", g.round, g.suppressed),
	})
}

func (e *Engine) checkIface(idx int, ev model.Event) bool {
	if !e.cfg.HasInterface(ev.Iface) {
		e.reject(idx, ev, cats.UnknownInterface, "interface "+ev.Iface+" is not declared in config")
		return false
	}
	return true
}

func (e *Engine) checkGroup(idx int, ev model.Event) bool {
	addr, err := netip.ParseAddr(ev.Group)
	if err != nil || !addr.IsMulticast() {
		e.reject(idx, ev, cats.InvalidGroup, "group "+ev.Group+" is not a valid multicast address")
		return false
	}
	return true
}

func (e *Engine) checkMember(idx int, ev model.Event) bool {
	if _, err := netip.ParseAddr(ev.Member); err != nil {
		e.reject(idx, ev, cats.InvalidMember, "member "+ev.Member+" is not a valid IP address")
		return false
	}
	return true
}

func (e *Engine) add(t Transition) {
	t.Seq = len(e.transitions)
	e.transitions = append(e.transitions, t)
}

func (e *Engine) reject(idx int, ev model.Event, category, reason string) {
	e.rejections = append(e.rejections, Rejection{
		EventIndex: idx, TimeMS: ev.TimeMS, Category: category,
		Iface: ev.Iface, Group: ev.Group, Member: ev.Member, Reason: reason,
	})
}

func (e *Engine) closeInterval(k Key, end int64) {
	key := k.String()
	list := e.intervals[key]
	if n := len(list); n > 0 && list[n-1].EndMS == nil {
		e.intervals[key][n-1].EndMS = &end
	}
}

// openInterval starts a new forwarding interval when a group is created.
func (e *Engine) openInterval(k Key, start int64) {
	key := k.String()
	e.intervals[key] = append(e.intervals[key], Interval{StartMS: start})
}
