// Package oracle is an INDEPENDENT reference implementation used only by
// tests. It re-parses a scenario JSON file with its own local types and
// re-derives the expected decisions with its own state machine. It must NOT
// import the code under test (no imports of internal/core, internal/engine,
// internal/config, internal/store, internal/netmodel). The expected answers
// are therefore not produced by the implementation being tested.
package oracle

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
)

// Timing is the oracle-local copy of RFC 2236 timer semantics.
type Timing struct {
	QI, QRI, GMI, LMQI int64
	LMQC               int
}

// Default RFC 2236 §8 values.
func defaultTiming() Timing {
	return Timing{QI: 125000, QRI: 10000, GMI: 135000, LMQI: 1000, LMQC: 2}
}

func (t Timing) overlay(o Timing) Timing {
	if o.QI != 0 {
		t.QI = o.QI
	}
	if o.QRI != 0 {
		t.QRI = o.QRI
	}
	if o.GMI != 0 {
		t.GMI = o.GMI
	}
	if o.LMQI != 0 {
		t.LMQI = o.LMQI
	}
	if o.LMQC != 0 {
		t.LMQC = o.LMQC
	}
	return t
}

// ---- local JSON schema (intentionally separate from engine types) ----

type jTiming struct {
	QI   int64 `json:"query_interval_ms"`
	QRI  int64 `json:"query_response_interval_ms"`
	GMI  int64 `json:"group_membership_interval_ms"`
	LMQI int64 `json:"last_member_query_interval_ms"`
	LMQC int   `json:"last_member_query_count"`
}

type jMember struct {
	Name            string  `json:"name"`
	Addr            string  `json:"addr"`
	Iface           string  `json:"iface"`
	GeneralDelayMs  int64   `json:"general_delay_ms"`
	GeneralDelaysMs []int64 `json:"general_delays_ms"`
	LMQDelayMs      int64   `json:"lmq_delay_ms"`
}

type jEvent struct {
	At          int64  `json:"at_ms"`
	Kind        string `json:"kind"`
	Member      string `json:"member"`
	Group       string `json:"group"`
	Iface       string `json:"iface"`
	ResponseTo  int64  `json:"response_to_gen"`
	RefGeneralN int    `json:"ref_general_n"`
	Deliver     *bool  `json:"deliver"`
	RequestID   string `json:"request_id"`
}

type jDrop struct {
	Packet string `json:"packet"`
	Iface  string `json:"iface"`
	Group  string `json:"group"`
	FromMs int64  `json:"from_ms"`
	Count  int    `json:"count"`
}

type jScript struct {
	Name    string    `json:"name"`
	Iface   string    `json:"iface"`
	Group   string    `json:"group"`
	Timing  jTiming   `json:"timing_override"`
	Members []jMember `json:"members"`
	Events  []jEvent  `json:"events"`
	Drops   []jDrop   `json:"drop_rules"`
	UntilMs int64     `json:"until_ms"`
}

// Diag is one expected diagnostic.
type Diag struct {
	At         int64
	Packet     string
	Member     string
	Verdict    string
	Reason     string
	GenActive  int64
	GenApplied int64
	Deadline   int64
	Members    []string
}

// Interval is an expected forwarding retention interval.
type Interval struct {
	Start, End int64
	Reason     string
}

// Expectation is the oracle's complete answer set.
type Expectation struct {
	Script       string
	FinalPresent bool
	FinalMembers []string
	Intervals    []Interval
	Diags        []Diag
	// GeneralQueryDroppedGens: generations of general queries lost in transit
	GeneralQueryDroppedGens []int64
	// GroupQueriesAt: emission times of group-specific queries
	GroupQueriesAt []int64
}

type lmq struct {
	sent      int
	nextAt    int64
	deadline  int64
	deleteGen int64
}

type group struct {
	members  map[string]bool
	deadline int64
	gen      int64 // membership generation
	lmq      *lmq
}

type pending struct {
	at     int64
	member string
	gen    int64
	qkind  string
}

// ExpectedFor parses scriptPath and computes the expected outcome.
func ExpectedFor(scriptPath string) (*Expectation, error) {
	raw, err := os.ReadFile(scriptPath)
	if err != nil {
		return nil, err
	}
	var js jScript
	if err := json.Unmarshal(raw, &js); err != nil {
		return nil, err
	}
	sort.SliceStable(js.Events, func(i, j int) bool { return js.Events[i].At < js.Events[j].At })

	t := defaultTiming().overlay(Timing{
		QI: js.Timing.QI, QRI: js.Timing.QRI, GMI: js.Timing.GMI,
		LMQI: js.Timing.LMQI, LMQC: js.Timing.LMQC,
	})

	e := &Expectation{Script: js.Name}

	// group state for the single scenario (iface, group)
	g := &group{members: map[string]bool{}}
	present := func() bool { return len(g.members) > 0 || g.lmq != nil }

	// host membership ledger (host-side, separate from router state)
	hostJoined := map[string]bool{}
	gDelay := map[string][]int64{}
	lDelay := map[string]int64{}
	for _, m := range js.Members {
		if len(m.GeneralDelaysMs) > 0 {
			gDelay[m.Name] = m.GeneralDelaysMs
		} else {
			gDelay[m.Name] = []int64{m.GeneralDelayMs}
		}
		lDelay[m.Name] = m.LMQDelayMs
	}
	generalDelay := func(member string, round int) int64 {
		ds := gDelay[member]
		if round < len(ds) {
			return ds[round]
		}
		return ds[len(ds)-1]
	}

	var gen int64 // current general-query generation on the interface
	var diags []Diag
	var intervals []Interval
	intervalStart := int64(-1)
	gqDelivered := 0
	dropUsed := make([]int, len(js.Drops))

	closeInterval := func(at int64, reason string) {
		if intervalStart >= 0 {
			intervals = append(intervals, Interval{
				Start: intervalStart, End: at, Reason: reason,
			})
			intervalStart = -1
		}
	}
	openInterval := func(at int64) {
		if intervalStart < 0 {
			intervalStart = at
		}
	}

	var pend []pending
	findReporter := func(gg int64) string {
		for _, d := range diags {
			if d.Packet == "REPORT_V2" && d.GenApplied == gg && d.Verdict == "ACCEPTED" {
				return d.Member
			}
		}
		return ""
	}
	memberList := func() []string {
		out := make([]string, 0, len(g.members))
		for m := range g.members {
			out = append(out, m)
		}
		sort.Strings(out)
		return out
	}

	emitGeneral := func(at int64, delivered bool) {
		gen++
		if !delivered {
			e.GeneralQueryDroppedGens = append(e.GeneralQueryDroppedGens, gen)
			diags = append(diags, Diag{At: at, Packet: "QUERY_GENERAL",
				Verdict: "DROPPED", Reason: "query_lost_in_transit", GenActive: gen})
			return
		}
		round := gqDelivered // zero-based index of this delivered query
		gqDelivered++
		// schedule reactive reports from joined hosts
		for _, m := range js.Members {
			if hostJoined[m.Name] {
				pend = append(pend, pending{
					at: at + generalDelay(m.Name, round), member: m.Name, gen: gen, qkind: "G",
				})
			}
		}
	}

	dropMatches := func(pkt string, at int64) bool {
		for i, dr := range js.Drops {
			if dr.Packet != pkt {
				continue
			}
			if dr.Iface != "" && dr.Iface != js.Iface {
				continue
			}
			if dr.Group != "" && dr.Group != js.Group {
				continue
			}
			if at < dr.FromMs {
				continue
			}
			if dr.Count != 0 && dropUsed[i] >= dr.Count {
				continue
			}
			dropUsed[i]++
			return true
		}
		return false
	}

	// emitGSQ emits one due group-specific query at gsqAt and, when
	// delivered, schedules reactive LMQ responses from joined hosts.
	emitGSQ := func(gsqAt int64) {
		e.GroupQueriesAt = append(e.GroupQueriesAt, gsqAt)
		diags = append(diags, Diag{At: gsqAt, Packet: "QUERY_GROUP",
			Verdict: "ACCEPTED", Reason: "group_specific_query_emitted",
			Deadline: g.deadline})
		if !dropMatches("QUERY_GROUP", gsqAt) {
			for _, m := range js.Members {
				if hostJoined[m.Name] {
					pend = append(pend, pending{
						at: gsqAt + lDelay[m.Name], member: m.Name,
						gen: g.lmq.deleteGen, qkind: "L",
					})
				}
			}
		}
	}

	// timers: emits due group-specific queries and applies deletions.
	advanceTimers := func(now int64) {
		if g.lmq != nil {
			for g.lmq.sent < t.LMQC && g.lmq.nextAt <= now {
				gsqAt := g.lmq.nextAt
				g.lmq.sent++
				g.lmq.nextAt += t.LMQI
				emitGSQ(gsqAt)
			}
		}
		// deletion — timestamped at the exact timer deadline so sweeps at
		// later event times yield the same boundary.
		if g.lmq != nil && now >= g.lmq.deadline {
			if g.gen != g.lmq.deleteGen {
				diags = append(diags, Diag{At: g.lmq.deadline, Packet: "QUERY_GROUP",
					Verdict: "STALE", Reason: "stale_query_round",
					GenActive: g.gen, GenApplied: g.lmq.deleteGen})
				g.lmq = nil
			} else {
				lmqDeadline := g.lmq.deadline
				diags = append(diags, Diag{At: lmqDeadline, Packet: "QUERY_GROUP",
					Verdict: "TIMEOUT", Reason: "last_member_query_confirmed"})
				g.members = map[string]bool{}
				g.lmq = nil
				g.gen = 0
				closeInterval(lmqDeadline, "last_member_query_confirmed")
			}
		} else if g.lmq == nil && len(g.members) > 0 && now >= g.deadline {
			diags = append(diags, Diag{At: g.deadline, Packet: "QUERY_GROUP",
				Verdict: "TIMEOUT", Reason: "membership_interval_expired"})
			g.members = map[string]bool{}
			g.gen = 0
			closeInterval(g.deadline, "membership_interval_expired")
		}
	}

	drainPending := func(now int64) {
		for {
			idx := -1
			for i, p := range pend {
				if p.at > now {
					continue
				}
				if idx == -1 || p.at < pend[idx].at ||
					(p.at == pend[idx].at && p.member < pend[idx].member) {
					idx = i
				}
			}
			if idx == -1 {
				return
			}
			p := pend[idx]
			pend = append(pend[:idx], pend[idx+1:]...)

			if rep := findReporter(p.gen); rep != "" {
				diags = append(diags, Diag{At: p.at, Packet: "REPORT_V2",
					Member: p.member, Verdict: "SUPPRESSED",
					Reason: "report_suppressed", GenActive: gen, GenApplied: p.gen})
				continue
			}

			// stale: group gone, answering an older general round
			if !present() && p.gen < gen && p.qkind == "G" {
				diags = append(diags, Diag{At: p.at, Packet: "REPORT_V2",
					Member: p.member, Verdict: "STALE",
					Reason: "stale_query_round", GenActive: gen, GenApplied: p.gen})
				continue
			}

			cancelled := g.lmq != nil
			created := len(g.members) == 0 && g.lmq == nil
			if created {
				g.gen = 1
				openInterval(p.at)
			}
			if cancelled {
				g.gen++
				g.lmq = nil
			}
			g.members[p.member] = true
			g.deadline = p.at + t.GMI
			d := Diag{At: p.at, Packet: "REPORT_V2", Member: p.member,
				Verdict: "ACCEPTED", Deadline: g.deadline,
				GenActive: gen, GenApplied: p.gen, Members: memberList()}
			switch {
			case cancelled:
				d.Reason = "report_during_lmq"
			case created:
				d.Reason = "membership_created"
			default:
				d.Reason = "membership_refreshed"
			}
			diags = append(diags, d)
		}
	}

	applyReportEvent := func(ev jEvent, now int64, responseTo int64, hasRef bool) {
		created := len(g.members) == 0 && g.lmq == nil
		if !present() && hasRef && responseTo < gen {
			diags = append(diags, Diag{At: now, Packet: "REPORT_V2", Member: ev.Member,
				Verdict: "STALE", Reason: "stale_query_round",
				GenActive: gen, GenApplied: responseTo})
			return
		}
		cancelled := g.lmq != nil
		if created {
			g.gen = 1
			openInterval(now)
		}
		if cancelled {
			g.gen++
			g.lmq = nil
		}
		already := g.members[ev.Member]
		g.members[ev.Member] = true
		g.deadline = now + t.GMI
		d := Diag{At: now, Packet: "REPORT_V2", Member: ev.Member,
			Verdict: "ACCEPTED", Deadline: g.deadline, Members: memberList()}
		if hasRef {
			d.GenActive = gen
			d.GenApplied = responseTo
		}
		switch {
		case cancelled:
			d.Reason = "report_during_lmq"
		case created:
			d.Reason = "membership_created"
		default:
			d.Reason = "membership_refreshed"
		}
		_ = already
		diags = append(diags, d)
	}

	applyLeaveEvent := func(ev jEvent, now int64) {
		if !present() {
			diags = append(diags, Diag{At: now, Packet: "LEAVE", Member: ev.Member,
				Verdict: "REJECTED", Reason: "leave_unknown_group"})
			return
		}
		if !g.members[ev.Member] {
			diags = append(diags, Diag{At: now, Packet: "LEAVE", Member: ev.Member,
				Verdict: "REJECTED", Reason: "leave_unknown_member",
				Deadline: g.deadline, Members: memberList()})
			return
		}
		delete(g.members, ev.Member)
		if len(g.members) > 0 {
			diags = append(diags, Diag{At: now, Packet: "LEAVE", Member: ev.Member,
				Verdict: "ACCEPTED", Reason: "member_left_group_retained",
				Deadline: g.deadline, Members: memberList()})
			return
		}
		g.lmq = &lmq{
			sent:      0,
			nextAt:    now, // first GSQ is due immediately at the leave
			deadline:  now + t.LMQI*int64(t.LMQC),
			deleteGen: g.gen,
		}
		diags = append(diags, Diag{At: now, Packet: "LEAVE", Member: ev.Member,
			Verdict: "ACCEPTED", Reason: "last_member_query_started",
			Deadline: g.deadline, Members: memberList()})
	}

	// afterLeaveEmits fires the immediate first GSQ for a leave handled at
	// the current timeline point (RFC 3376), before host responses.
	afterLeaveEmits := func(now int64) {
		advanceTimers(now)
	}

	// ---- timeline (same ordering contract as the engine) ----
	idx := 0
	for idx < len(js.Events) || len(pend) > 0 {
		nextScript := int64(1<<62 - 1)
		if idx < len(js.Events) {
			nextScript = js.Events[idx].At
		}
		nextPending := int64(1<<62 - 1)
		if len(pend) > 0 {
			for _, p := range pend {
				if p.at < nextPending {
					nextPending = p.at
				}
			}
		}
		now := nextScript
		if nextPending < now {
			now = nextPending
		}
		if now > js.UntilMs {
			break
		}
		advanceTimers(now)
		drainPending(now)

		// process every script event scheduled at now (preserving order)
		hadLeave := false
		for idx < len(js.Events) && js.Events[idx].At == now {
			ev := js.Events[idx]
			idx++
			iface := ev.Iface
			if iface == "" {
				iface = js.Iface
			}
			grp := ev.Group
			if grp == "" {
				grp = js.Group
			}
			switch ev.Kind {
			case "checkpoint", "advance":
				// nothing
			case "force_query":
				lost := (ev.Deliver != nil && !*ev.Deliver) || dropMatches("QUERY_GENERAL", now)
				emitGeneral(now, !lost)
			case "report":
				hostJoined[ev.Member] = true
				hasRef := false
				ref := int64(0)
				switch {
				case ev.RefGeneralN > 0:
					// Nth delivered general query: count delivered gens
					n := int64(0)
					var found int64
					for gg := int64(1); gg <= gen; gg++ {
						isDropped := false
						for _, dg := range e.GeneralQueryDroppedGens {
							if dg == gg {
								isDropped = true
							}
						}
						if !isDropped {
							n++
							if int(ev.RefGeneralN) == int(n) {
								found = gg
							}
						}
					}
					if found > 0 {
						hasRef, ref = true, found
					}
				case ev.ResponseTo > 0:
					hasRef, ref = true, ev.ResponseTo
				}
				applyReportEvent(ev, now, ref, hasRef)
			case "leave":
				delete(hostJoined, ev.Member)
				applyLeaveEvent(ev, now)
				hadLeave = true
			}
		}
		// A leave emits its first GSQ immediately at the same time; deliver
		// scheduled host responses afterwards (matches the engine ordering).
		if hadLeave {
			afterLeaveEmits(now)
			drainPending(now)
		}
	}
	advanceTimers(js.UntilMs)
	drainPending(js.UntilMs)

	e.FinalPresent = present()
	e.FinalMembers = memberList()
	e.Intervals = intervals
	e.Diags = diags
	sort.Slice(e.GroupQueriesAt, func(i, j int) bool { return e.GroupQueriesAt[i] < e.GroupQueriesAt[j] })
	return e, nil
}

// FindVerdict is a small helper for tests: returns the first expected diag
// matching (packet, verdict, member, reasonPrefix optional).
func (e *Expectation) FindVerdict(verdict, member, reason string) *Diag {
	for i := range e.Diags {
		d := &e.Diags[i]
		if d.Verdict != verdict {
			continue
		}
		if member != "" && d.Member != member {
			continue
		}
		if reason != "" && d.Reason != reason {
			continue
		}
		return d
	}
	return nil
}

// String is a compact human-readable rendering for failure messages.
func (d Diag) String() string {
	return fmt.Sprintf("t=%d %s member=%s %s/%s gen(a=%d,p=%d) deadline=%d members=%v",
		d.At, d.Packet, d.Member, d.Verdict, d.Reason,
		d.GenActive, d.GenApplied, d.Deadline, d.Members)
}
