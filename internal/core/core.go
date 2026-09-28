// Package core is the IGMPv2 membership/query timer state machine — the
// component under test.
//
// It models the router-side host-facing timers of RFC 2236:
//
//   - A Membership Report for (interface, group) creates/refreshes the
//     forwarding entry; its membership deadline is now+GMI (Group
//     Membership Interval).
//   - A Leave removes one member. While any member remains the entry is
//     retained; when the last member leaves, Last-Member-Query procedure
//     runs: LMQC group-specific queries spaced LMQI apart, after which the
//     entry is deleted unless a report cancels the procedure.
//   - General queries carry a per-interface generation tag. Reports that
//     answer an older, superseded round after the group has gone are
//     classified STALE and never recreate state.
//
// This is NOT a multicast routing implementation: there is no querier
// election, no RIB/PIM and no real sockets — only membership bookkeeping.
package core

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"

	"igmpv2timer/internal/clock"
	"igmpv2timer/internal/config"
	"igmpv2timer/internal/model"
)

type member struct {
	Name string
	Addr string
}

type lmqState struct {
	sent       int
	nextAt     model.Millis // when the next GSQ is due
	deadline   model.Millis // when the group is deleted
	deleteGen  int64        // membership gen that the leave belonged to
	requestIDs []string     // request ids of the GSQs emitted
}

type groupState struct {
	iface    string
	group    string
	members  map[string]member // keyed by member name
	deadline model.Millis      // Group Membership Interval deadline
	gen      int64             // membership generation (bumped per 0→1 join)
	lmq      *lmqState
	interval model.Interval // current/last forwarding retention interval
}

// Core is the state machine.
type Core struct {
	mu        sync.Mutex
	cfg       config.Config
	clk       *clock.Clock
	ifaces    map[string]bool
	groups    map[string]*groupState  // key: iface + "\x00" + group
	gen       map[string]int64        // general-query generation per iface
	nextGen   map[string]model.Millis // next periodic general-query time
	diags     []model.Diag
	emitted   []model.EmittedPkt
	intervals []model.Interval
	seq       int64
}

// New builds a Core for cfg using the given injected clock.
func New(cfg config.Config, clk *clock.Clock) (*Core, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	c := &Core{
		cfg:     cfg,
		clk:     clk,
		ifaces:  map[string]bool{},
		groups:  map[string]*groupState{},
		gen:     map[string]int64{},
		nextGen: map[string]model.Millis{},
	}
	for _, ifc := range cfg.Interfaces {
		c.ifaces[ifc.Name] = true
		c.gen[ifc.Name] = 0
		c.nextGen[ifc.Name] = model.Millis(cfg.Timing.QueryInterval)
	}
	return c, nil
}

func groupKey(iface, group string) string { return iface + "\x00" + group }

// now must be called with c held (clock itself is also safe).
func (c *Core) now() model.Millis { return c.clk.Now() }

func (c *Core) nextSeq() int64 { c.seq++; return c.seq }

func (c *Core) memberList(g *groupState) []string {
	out := make([]string, 0, len(g.members))
	for n := range g.members {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// strsOrEmpty guarantees a non-nil slice for stable JSON.
func strsOrEmpty(in []string) []string {
	if in == nil {
		return []string{}
	}
	return in
}

func (c *Core) baseDiag(ev model.Event, pkt model.PacketType, v model.Verdict) model.Diag {
	return model.Diag{
		RequestID: ev.RequestID,
		Seq:       c.nextSeq(),
		At:        c.now(),
		Iface:     ev.Iface,
		Group:     ev.Group,
		Member:    ev.Member,
		Packet:    pkt,
		Verdict:   v,
	}
}

func (c *Core) snapshotOf(g *groupState) model.GroupSnapshot {
	s := model.GroupSnapshot{
		Iface:              g.iface,
		Group:              g.group,
		Present:            true,
		Members:            strsOrEmpty(c.memberList(g)),
		MembershipDeadline: g.deadline,
		DeleteGen:          g.gen,
	}
	if g.lmq != nil {
		s.LMQActive = true
		s.LMQSent = g.lmq.sent
		s.LMQDeadline = g.lmq.deadline
		s.DeleteGen = g.lmq.deleteGen
	}
	return s
}

func (c *Core) attachDeadline(d *model.Diag, g *groupState) {
	if g != nil {
		d.MembershipDeadline = g.deadline
		d.Members = c.memberList(g)
	}
}

// Apply processes one membership event (report/leave) at the clock's current
// time. The caller (engine) is responsible for advancing the clock first.
func (c *Core) Apply(ev model.Event) model.Diag {
	c.mu.Lock()
	defer c.mu.Unlock()

	switch ev.Kind {
	case model.EvReport:
		return c.applyReport(ev)
	case model.EvLeave:
		return c.applyLeave(ev)
	default:
		d := c.baseDiag(ev, "", model.VRejected)
		d.Reason = "unsupported_event_kind"
		d.Detail = fmt.Sprintf("kind=%s", ev.Kind)
		return d
	}
}

func (c *Core) validate(ev model.Event, pkt model.PacketType) (model.Diag, bool) {
	if !c.ifaces[ev.Iface] {
		d := c.baseDiag(ev, pkt, model.VRejected)
		d.Reason = "unknown_interface"
		d.Detail = fmt.Sprintf("interface %q is not configured", ev.Iface)
		return d, false
	}
	if _, err := model.ValidateGroupAddr(ev.Group); err != nil {
		d := c.baseDiag(ev, pkt, model.VRejected)
		d.Reason = model.ReasonBadGroupAddr
		d.Detail = err.Error()
		return d, false
	}
	if _, err := model.ValidateSourceAddr(ev.SourceAddr); err != nil {
		d := c.baseDiag(ev, pkt, model.VRejected)
		d.Reason = model.ReasonBadSourceAddr
		d.Detail = err.Error()
		return d, false
	}
	return model.Diag{}, true
}

// resolveResponseGen interprets ev.ResponseTo: empty = unsolicited; an
// integer generation reference otherwise. Returns (gen, hasRef, okDiag).
func (c *Core) resolveResponseGen(ev model.Event) (int64, bool, model.Diag) {
	ref := strings.TrimSpace(ev.ResponseTo)
	if ref == "" {
		return 0, false, model.Diag{}
	}
	n, err := strconv.ParseInt(ref, 10, 64)
	if err != nil || n <= 0 {
		d := c.baseDiag(ev, model.PktReportV2, model.VUndecidable)
		d.Reason = model.ReasonGenerationUnknown
		d.Detail = fmt.Sprintf("response_to=%q is not a valid generation", ev.ResponseTo)
		return 0, true, d
	}
	cur := c.gen[ev.Iface]
	if n > cur {
		d := c.baseDiag(ev, model.PktReportV2, model.VUndecidable)
		d.Reason = model.ReasonGenerationUnknown
		d.Detail = fmt.Sprintf("response_to gen %d does not exist yet (current gen %d)", n, cur)
		d.GenActive = cur
		d.GenApplied = n
		return n, true, d
	}
	return n, true, model.Diag{}
}

func (c *Core) applyReport(ev model.Event) model.Diag {
	if d, ok := c.validate(ev, model.PktReportV2); !ok {
		return d
	}
	refGen, hasRef, bad := c.resolveResponseGen(ev)
	if bad.RequestID != "" || bad.At != 0 || bad.Verdict != "" {
		return bad
	}

	key := groupKey(ev.Iface, ev.Group)
	g, exists := c.groups[key]
	curGen := c.gen[ev.Iface]

	// Stale round: the group is gone and the report answers a query round
	// that a newer round has already superseded. It must not recreate state.
	if !exists && hasRef && refGen < curGen {
		d := c.baseDiag(ev, model.PktReportV2, model.VStale)
		d.Reason = model.ReasonStaleRound
		d.Detail = fmt.Sprintf("report answers gen %d but gen %d is current and group has expired",
			refGen, curGen)
		d.GenActive = curGen
		d.GenApplied = refGen
		return d
	}

	created := false
	cancelledLMQ := false
	if !exists {
		g = &groupState{
			iface:   ev.Iface,
			group:   ev.Group,
			members: map[string]member{},
			gen:     1,
			interval: model.Interval{
				Iface: ev.Iface, Group: ev.Group, Start: c.now(),
			},
		}
		c.groups[key] = g
		c.intervals = append(c.intervals, g.interval)
		created = true
	}
	if g.lmq != nil {
		// A report while the last-member sequence is running opens a new
		// membership generation and cancels deletion.
		g.gen++
		g.lmq = nil
		cancelledLMQ = true
	}

	name := ev.Member
	if name == "" {
		name = ev.SourceAddr
	}
	_, already := g.members[name]
	g.members[name] = member{Name: name, Addr: ev.SourceAddr}
	g.deadline = c.now() + model.Millis(c.cfg.Timing.GroupMembershipInterval)

	d := c.baseDiag(ev, model.PktReportV2, model.VAccepted)
	switch {
	case cancelledLMQ:
		d.Reason = model.ReasonReportDuringLMQ
		d.Detail = "report received during last-member query; deletion cancelled"
	case created:
		d.Reason = model.ReasonMembershipCreated
	case already:
		d.Reason = model.ReasonMembershipRefreshed
		d.Detail = "duplicate report from existing member; deadline refreshed"
	default:
		d.Reason = model.ReasonMembershipRefreshed
	}
	d.Detail = strings.TrimSpace(d.Detail +
		fmt.Sprintf("; src=%s; deadline=+%dms",
			model.MaskAddr(ev.SourceAddr), c.cfg.Timing.GroupMembershipInterval))
	if hasRef {
		d.GenApplied = refGen
		d.GenActive = curGen
	}
	c.attachDeadline(&d, g)
	return d
}

func (c *Core) applyLeave(ev model.Event) model.Diag {
	if d, ok := c.validate(ev, model.PktLeave); !ok {
		return d
	}
	key := groupKey(ev.Iface, ev.Group)
	g, exists := c.groups[key]
	if !exists {
		d := c.baseDiag(ev, model.PktLeave, model.VRejected)
		d.Reason = model.ReasonLeaveNoGroup
		d.Detail = fmt.Sprintf("leave for group %s with no forwarding entry", ev.Group)
		return d
	}
	name := ev.Member
	if name == "" {
		name = ev.SourceAddr
	}
	if _, ok := g.members[name]; !ok {
		d := c.baseDiag(ev, model.PktLeave, model.VRejected)
		d.Reason = model.ReasonLeaveUnknownMember
		d.Detail = fmt.Sprintf("member %q (src=%s) has no membership for %s",
			name, model.MaskAddr(ev.SourceAddr), ev.Group)
		c.attachDeadline(&d, g)
		return d
	}
	delete(g.members, name)

	d := c.baseDiag(ev, model.PktLeave, model.VAccepted)
	if len(g.members) > 0 {
		// Other members still hold the group: the forwarding entry must be
		// retained and no last-member query is started.
		d.Reason = "member_left_group_retained"
		d.Detail = fmt.Sprintf("%d member(s) remain; entry retained", len(g.members))
		c.attachDeadline(&d, g)
		return d
	}

	// Last member gone → start the last-member-query procedure (RFC 3376
	// §6.3 / RFC 2236 §7): the FIRST group-specific query is sent
	// immediately, LMQC-1 retransmissions follow at LMQI spacing, and the
	// group is deleted at leave + LMQC*LMQI if no report cancels it.
	lmqc := c.cfg.Timing.LastMemberQueryCount
	g.lmq = &lmqState{
		sent:      0,
		nextAt:    c.now(), // first GSQ due now; emitted by the same-time tick
		deadline:  c.now() + model.Millis(c.cfg.Timing.LastMemberQueryInterval)*model.Millis(lmqc),
		deleteGen: g.gen,
	}
	d.Reason = model.ReasonLastMemberStarted
	d.Detail = fmt.Sprintf("last member left; %d group-specific queries, delete at %d if unanswered",
		lmqc, g.lmq.deadline)
	c.attachDeadline(&d, g)
	return d
}

// InjectGeneralQuery models a General Query observed on the interface
// (periodic emission or script-injected). It opens a new query generation.
// The clock must already be at t.
func (c *Core) InjectGeneralQuery(iface string, requestID string) (model.EmittedPkt, model.Diag, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.ifaces[iface] {
		return model.EmittedPkt{}, model.Diag{}, fmt.Errorf("unknown interface %q", iface)
	}
	c.gen[iface]++
	gen := c.gen[iface]
	pkt := model.EmittedPkt{
		At:        c.now(),
		Iface:     iface,
		Packet:    model.PktQueryGeneral,
		Gen:       gen,
		RequestID: requestID,
	}
	c.emitted = append(c.emitted, pkt)
	d := model.Diag{
		RequestID: requestID,
		Seq:       c.nextSeq(),
		At:        c.now(),
		Iface:     iface,
		Packet:    model.PktQueryGeneral,
		Verdict:   model.VAccepted,
		Reason:    model.ReasonGeneralQueryEmitted,
		Detail:    fmt.Sprintf("general query gen %d opened", gen),
		GenActive: gen,
	}
	return pkt, d, nil
}

// Tick advances the injected clock to t and fires all due timers
// (periodic general queries, group-specific queries, membership timeouts).
// Emitted packets and timer diagnostics are returned in deterministic order.
func (c *Core) Tick(t model.Millis) ([]model.EmittedPkt, []model.Diag, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.clk.Advance(t); err != nil {
		return nil, nil, err
	}

	var emitted []model.EmittedPkt
	var diags []model.Diag

	// 1) Periodic general queries (deterministic interface order).
	ifNames := make([]string, 0, len(c.ifaces))
	for n := range c.ifaces {
		ifNames = append(ifNames, n)
	}
	sort.Strings(ifNames)
	for _, n := range ifNames {
		qi := model.Millis(c.cfg.Timing.QueryInterval)
		for c.nextGen[n] <= t {
			c.gen[n]++
			pkt := model.EmittedPkt{
				At: t /* emitted at its scheduled time, see Detail */, Iface: n,
				Packet: model.PktQueryGeneral, Gen: c.gen[n],
			}
			pkt.At = c.nextGen[n]
			c.emitted = append(c.emitted, pkt)
			emitted = append(emitted, pkt)
			c.nextGen[n] += qi
		}
	}

	// 2) Per-group timers, in key order for deterministic output.
	keys := make([]string, 0, len(c.groups))
	for k := range c.groups {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		g := c.groups[k]
		// 2a) group-specific queries due inside an LMQ sequence.
		if g.lmq != nil {
			lmqi := model.Millis(c.cfg.Timing.LastMemberQueryInterval)
			for g.lmq.sent < c.cfg.Timing.LastMemberQueryCount && g.lmq.nextAt <= t {
				pkt := model.EmittedPkt{
					At: g.lmq.nextAt, Iface: g.iface, Group: g.group,
					Packet: model.PktQueryGroup, Gen: g.lmq.deleteGen,
				}
				c.emitted = append(c.emitted, pkt)
				emitted = append(emitted, pkt)
				g.lmq.sent++
				g.lmq.requestIDs = append(g.lmq.requestIDs, "")
				g.lmq.nextAt += lmqi
				if g.lmq.sent == c.cfg.Timing.LastMemberQueryCount {
					g.lmq.deadline = g.lmq.nextAt
				}
				d := model.Diag{
					Seq: c.nextSeq(), At: pkt.At, Iface: g.iface, Group: g.group,
					Packet: model.PktQueryGroup, Verdict: model.VAccepted,
					Reason: model.ReasonGroupQueryEmitted,
					Detail: fmt.Sprintf("group-specific query %d/%d for %s",
						g.lmq.sent, c.cfg.Timing.LastMemberQueryCount, g.group),
					MembershipDeadline: g.deadline,
				}
				diags = append(diags, d)
			}
		}
		// 2b) deletion. Diag/interval timestamps use the exact timer
		// deadline (not the tick time that discovered it), so a rebuild
		// that only sweeps at event times records the same boundary.
		switch {
		case g.lmq != nil && t >= g.lmq.deadline:
			if g.gen != g.lmq.deleteGen {
				// Generation guard: a newer membership generation answered;
				// this old LMQ round must not delete the fresh entry.
				d := model.Diag{
					Seq: c.nextSeq(), At: g.lmq.deadline, Iface: g.iface, Group: g.group,
					Packet: model.PktQueryGroup, Verdict: model.VStale,
					Reason: model.ReasonStaleRound,
					Detail: fmt.Sprintf("LMQ of gen %d superseded by gen %d; deletion discarded",
						g.lmq.deleteGen, g.gen),
				}
				diags = append(diags, d)
				g.lmq = nil
				continue
			}
			c.deleteGroup(g, model.VTimeout, model.ReasonLastMemberConfirmed,
				fmt.Sprintf("no report after %d group-specific queries; forwarding entry removed",
					c.cfg.Timing.LastMemberQueryCount), g.lmq.deadline, &diags)
		case g.lmq == nil && t >= g.deadline:
			c.deleteGroup(g, model.VTimeout, model.ReasonMembershipTimeout,
				fmt.Sprintf("no report within Group Membership Interval (%dms)",
					c.cfg.Timing.GroupMembershipInterval), g.deadline, &diags)
		}
	}
	return emitted, diags, nil
}

// deleteGroup closes the forwarding interval and removes the group.
func (c *Core) deleteGroup(g *groupState, v model.Verdict, reason, detail string,
	at model.Millis, diags *[]model.Diag) {
	g.interval.End = at
	g.interval.Reason = reason
	for i := range c.intervals {
		if c.intervals[i].Iface == g.iface &&
			c.intervals[i].Group == g.group && c.intervals[i].Start == g.interval.Start {
			c.intervals[i] = g.interval
		}
	}
	delete(c.groups, groupKey(g.iface, g.group))
	*diags = append(*diags, model.Diag{
		Seq: c.nextSeq(), At: at, Iface: g.iface, Group: g.group,
		Packet: model.PktQueryGroup, Verdict: v, Reason: reason, Detail: detail,
	})
}

// Snapshot returns the current forwarding/membership table.
func (c *Core) Snapshot() model.StateSnapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	snap := model.StateSnapshot{At: c.now(), Groups: []model.GroupSnapshot{}}
	keys := make([]string, 0, len(c.groups))
	for k := range c.groups {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		snap.Groups = append(snap.Groups, c.snapshotOf(c.groups[k]))
	}
	return snap
}

// Intervals returns all forwarding-entry retention intervals (closed and open).
func (c *Core) Intervals() []model.Interval {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]model.Interval, len(c.intervals))
	copy(out, c.intervals)
	sort.Slice(out, func(i, j int) bool {
		if out[i].Iface != out[j].Iface {
			return out[i].Iface < out[j].Iface
		}
		if out[i].Group != out[j].Group {
			return out[i].Group < out[j].Group
		}
		return out[i].Start < out[j].Start
	})
	return out
}

// CurrentGen returns the current general-query generation on iface.
func (c *Core) CurrentGen(iface string) int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.gen[iface]
}

// EmittedPackets returns all query packets the router has emitted, in
// time order.
func (c *Core) EmittedPackets() []model.EmittedPkt {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]model.EmittedPkt, len(c.emitted))
	copy(out, c.emitted)
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].At != out[j].At {
			return out[i].At < out[j].At
		}
		if out[i].Iface != out[j].Iface {
			return out[i].Iface < out[j].Iface
		}
		return string(out[i].Packet) < string(out[j].Packet)
	})
	return out
}
