package engine

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"igmpv2timer/internal/clock"
	"igmpv2timer/internal/config"
	"igmpv2timer/internal/core"
	"igmpv2timer/internal/model"
	"igmpv2timer/internal/netmodel"
	"igmpv2timer/internal/store"
)

// Failure categories for assertion evaluation (stable strings the
// independent tests assert on).
const (
	FailExpectedPresent  = "FAIL_GROUP_EXPECTED_PRESENT"
	FailExpectedAbsent   = "FAIL_GROUP_EXPECTED_ABSENT"
	FailDiagNotFound     = "FAIL_DIAGNOSTIC_NOT_FOUND"
	FailDiagMultiple     = "FAIL_DIAGNOSTIC_AMBIGUOUS"
	FailCountMismatch    = "FAIL_PACKET_COUNT_MISMATCH"
	FailIntervalMismatch = "FAIL_RETENTION_INTERVAL_MISMATCH"
	FailGenMismatch      = "FAIL_TIMER_GENERATION_MISMATCH"
)

type pendingResp struct {
	at     model.Millis
	iface  string
	group  string
	member string
	addr   string
	gen    int64 // query generation the host answers
	qkind  model.PacketType
}

type dropCounter struct {
	rule DropRule
	used int
}

// Runner executes one scenario.
type Runner struct {
	cfg    config.Config
	script *Script
	clk    *clock.Clock
	core   *core.Core
	fabric *netmodel.Fabric
	st     *store.Store

	gDelays map[string][]int64 // member name -> per-round general delays
	lDelay  map[string]int64   // member name -> LMQ delay

	pending []*pendingResp
	drops   []dropCounter
	snaps   map[int64]model.StateSnapshot // explicit checkpoints
	history map[int64]model.StateSnapshot // post-step state at every timeline point
	gqCount int                           // general queries delivered so far

	report *Report
	seq    int64
}

// NewRunner builds a Runner by overlaying script timing onto baseCfg.
func NewRunner(baseCfg config.Config, s *Script, st *store.Store) (*Runner, error) {
	cfg := baseCfg
	cfg.Timing = baseCfg.Timing.MergeOverlay(s.Timing)
	if !cfg.HasInterface(s.Iface) {
		return nil, fmt.Errorf("scenario %q: interface %q not in config", s.Name, s.Iface)
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	clk := clock.New()
	c, err := core.New(cfg, clk)
	if err != nil {
		return nil, err
	}
	r := &Runner{
		cfg:     cfg,
		script:  s,
		clk:     clk,
		core:    c,
		fabric:  netmodel.NewFabric(),
		st:      st,
		gDelays: map[string][]int64{},
		lDelay:  map[string]int64{},
		snaps:   map[int64]model.StateSnapshot{},
		history: map[int64]model.StateSnapshot{},
		report:  &Report{Script: s.Name, Snapshots: map[int64]model.StateSnapshot{}},
	}
	r.fabric.AddLAN(s.Iface)
	for _, m0 := range s.Members {
		m := s.normalizedMember(m0)
		if m.Iface != s.Iface {
			return nil, fmt.Errorf("member %q: iface %q differs from scenario interface %q",
				m.Name, m.Iface, s.Iface)
		}
		if _, err := model.ValidateSourceAddr(m.Addr); err != nil {
			return nil, fmt.Errorf("member %q: %w", m.Name, err)
		}
		r.fabric.RegisterHost(m.Iface, m.Name, m.Addr)
		if m.GeneralDelayMs > cfg.Timing.QueryResponseInterval {
			return nil, fmt.Errorf("member %q: general delay %d > QRI %d",
				m.Name, m.GeneralDelayMs, cfg.Timing.QueryResponseInterval)
		}
		for ri, d := range m.GeneralDelaysMs {
			if d > cfg.Timing.QueryResponseInterval {
				return nil, fmt.Errorf("member %q: round %d delay %d > QRI %d",
					m.Name, ri, d, cfg.Timing.QueryResponseInterval)
			}
		}
		if m.LMQDelayMs > cfg.Timing.LastMemberQueryInterval {
			return nil, fmt.Errorf("member %q: lmq delay %d > LMQI %d",
				m.Name, m.LMQDelayMs, cfg.Timing.LastMemberQueryInterval)
		}
		delays := m.GeneralDelaysMs
		if len(delays) == 0 {
			delays = []int64{m.GeneralDelayMs}
		}
		r.gDelays[m.Name] = delays
		r.lDelay[m.Name] = m.LMQDelayMs
	}
	for _, d := range s.Drops {
		r.drops = append(r.drops, dropCounter{rule: d})
	}
	return r, nil
}

func (r *Runner) nextSeq() int64 { r.seq++; return r.seq }

func (r *Runner) recordDiag(d model.Diag) {
	if d.Seq == 0 {
		d.Seq = r.nextSeq()
	}
	if d.At == 0 {
		d.At = r.clk.Now()
	}
	r.report.Diags = append(r.report.Diags, d)
	if r.st != nil {
		_ = r.st.AppendDiag(d)
	}
}

func rid(prefix string, n int) string {
	return prefix + "-" + strconv.Itoa(n)
}

// Config returns the effective (overlay-merged) configuration of the run.
func (r *Runner) Config() config.Config { return r.cfg }

// Run executes the complete scenario and evaluates assertions.
func (r *Runner) Run() (*Report, error) {
	if r.st != nil {
		raw, _ := json.Marshal(r.cfg)
		if _, err := r.st.BeginRun(r.script.Name, raw); err != nil {
			return nil, err
		}
	}

	eventIdx := 0
	timerSeq := 0
	events := r.script.Events
	until := model.Millis(r.script.UntilMs)

	// Timeline loop: at each distinct point process core timers, then due
	// host responses, then every script event scheduled there.
	for eventIdx < len(events) || len(r.pending) > 0 {
		nextScript := model.Millis(1<<62 - 1)
		if eventIdx < len(events) {
			nextScript = model.Millis(events[eventIdx].At)
		}
		nextPending := model.Millis(1<<62 - 1)
		for _, p := range r.pending { // pending is not kept sorted; take min
			if p.at < nextPending {
				nextPending = p.at
			}
		}
		t := nextScript
		if nextPending < t {
			t = nextPending
		}
		if t > until {
			break
		}

		// 1) core timers (periodic general queries, GSQs, timeouts).
		emitted, timerDiags, err := r.core.Tick(t)
		if err != nil {
			return nil, err
		}
		for _, d := range timerDiags {
			timerSeq++
			d.RequestID = rid("timer", timerSeq)
			r.recordDiag(d)
		}
		for _, p := range emitted {
			if err := r.handleEmittedQuery(p); err != nil {
				return nil, err
			}
		}

		// 2) all host responses due at t (sorted, with suppression).
		r.drainPending(t)

		// 3) every script event scheduled at t (reports/leaves/checkpoints
		//    may share a timestamp; script order among them is preserved).
		for eventIdx < len(events) && model.Millis(events[eventIdx].At) == t {
			if err := r.applyScriptEvent(events[eventIdx], eventIdx+1); err != nil {
				return nil, err
			}
			eventIdx++
		}

		// 3b) A leave may start an LMQ whose FIRST group-specific query is
		// due immediately at t (RFC 3376). Sweep timers again at t so that
		// query is emitted now, then deliver any host responses it schedules
		// — before recording post-step state.
		emitted2, timerDiags2, err := r.core.Tick(t)
		if err != nil {
			return nil, err
		}
		for _, d := range timerDiags2 {
			timerSeq++
			d.RequestID = rid("timer", timerSeq)
			r.recordDiag(d)
		}
		for _, p := range emitted2 {
			if err := r.handleEmittedQuery(p); err != nil {
				return nil, err
			}
		}
		r.drainPending(t)

		// 4) record post-step state for point-in-time assertions.
		r.history[int64(t)] = r.core.Snapshot()
	}

	// Final advance to until fires boundary timeouts exactly.
	if r.clk.Now() < until {
		emitted, timerDiags, err := r.core.Tick(until)
		if err != nil {
			return nil, err
		}
		for _, p := range emitted {
			if err := r.handleEmittedQuery(p); err != nil {
				return nil, err
			}
		}
		for _, d := range timerDiags {
			r.recordDiag(d)
		}
	}
	r.drainPending(until)
	r.history[int64(until)] = r.core.Snapshot()

	r.report.Emitted = r.emittedPackets()
	r.report.Intervals = r.core.Intervals()
	if r.st != nil {
		for _, iv := range r.report.Intervals {
			_ = r.st.UpsertInterval(iv)
		}
	}
	r.report.Snapshots = r.snaps
	r.report.FinalSnapshot = r.core.Snapshot()
	r.evaluate()
	return r.report, nil
}

func (r *Runner) schedule(queryAt model.Millis, mem netmodel.Member, group string,
	gen int64, qkind model.PacketType, delay int64) {
	r.pending = append(r.pending, &pendingResp{
		at:     queryAt + model.Millis(delay),
		iface:  r.script.Iface,
		group:  group,
		member: mem.Name,
		addr:   mem.Addr,
		gen:    gen,
		qkind:  qkind,
	})
}

// drainPending delivers all responses due at/before t. Among responses for
// the same (group, query generation) the earliest (ties: member name)
// report is delivered; the rest are SUPPRESSED (IGMPv2 §3 host behavior).
func (r *Runner) drainPending(t model.Millis) {
	// pending is unsorted; repeatedly pull the earliest response and process
	// it while it is due at/before t.
	for len(r.pending) > 0 {
		idx := 0
		for i := 1; i < len(r.pending); i++ {
			if r.pending[i].at < r.pending[idx].at ||
				(r.pending[i].at == r.pending[idx].at &&
					r.pending[i].member < r.pending[idx].member) {
				idx = i
			}
		}
		p := r.pending[idx]
		if p.at > t {
			return
		}
		r.pending = append(r.pending[:idx], r.pending[idx+1:]...)

		// Suppression: an earlier delivered report for the same group+round
		// cancels this one.
		suppressedBy := r.findReporter(p.group, p.gen)
		if suppressedBy != "" {
			d := model.Diag{
				RequestID: rid("host", int(r.seq)+1),
				Seq:       r.nextSeq(),
				At:        p.at,
				Iface:     p.iface,
				Group:     p.group,
				Member:    p.member,
				Packet:    model.PktReportV2,
				Verdict:   model.VSuppressed,
				Reason:    model.ReasonReportSuppressed,
				Detail: fmt.Sprintf("member %s heard report from %s for %s (gen %d); its report is cancelled",
					p.member, suppressedBy, p.group, p.gen),
				GenApplied: p.gen,
				GenActive:  r.core.CurrentGen(p.iface),
			}
			r.recordDiag(d)
			continue
		}

		ev := model.Event{
			Seq:        r.nextSeq(),
			At:         p.at,
			Kind:       model.EvReport,
			Iface:      p.iface,
			Group:      p.group,
			Member:     p.member,
			SourceAddr: p.addr,
			ResponseTo: strconv.FormatInt(p.gen, 10),
			RequestID:  "resp-" + p.member + "-g" + strconv.FormatInt(p.gen, 10),
		}
		d := r.core.Apply(ev)
		if d.Verdict == model.VAccepted {
			// Journal the state-changing report as an unsolicited report:
			// generation tags are a network-round concept and the rebuild
			// re-derives timer state from membership events alone.
			j := ev
			j.ResponseTo = ""
			r.journalInput(j)
		}
		r.recordDiag(d)
	}
}

// findReporter returns the member whose report for group/gen has already
// been delivered (membership ledger containing that member is not enough;
// we track deliveries in diagnostics of ACCEPTED/SUPPRESSED reports).
func (r *Runner) findReporter(group string, gen int64) string {
	for _, d := range r.report.Diags {
		if d.Packet != model.PktReportV2 || d.Group != group {
			continue
		}
		if d.GenApplied == gen && (d.Verdict == model.VAccepted) {
			return d.Member
		}
	}
	return ""
}

// handleEmittedQuery records an emitted query, applies fixture drop rules,
// and on delivery schedules reactive host responses.
func (r *Runner) handleEmittedQuery(p model.EmittedPkt) error {
	if r.st != nil {
		_ = r.st.AppendEmitted(p)
	}
	deliver := true
	for i := range r.drops {
		dc := &r.drops[i]
		d := dc.rule
		if d.Packet != string(p.Packet) ||
			(d.Iface != "" && d.Iface != p.Iface) ||
			(d.Group != "" && d.Group != p.Group) ||
			int64(p.At) < d.FromMs ||
			(d.Count != 0 && dc.used >= d.Count) {
			continue
		}
		dc.used++
		deliver = false
		break
	}
	if !deliver {
		r.recordDiag(model.Diag{
			Seq:       r.nextSeq(),
			RequestID: p.RequestID,
			At:        p.At,
			Iface:     p.Iface,
			Group:     p.Group,
			Packet:    p.Packet,
			Verdict:   model.VDropped,
			Reason:    model.ReasonQueryDropped,
			Detail: fmt.Sprintf("%s gen %d emitted at %d but lost in transit by fixture drop rule",
				p.Packet, p.Gen, p.At),
			GenActive: p.Gen,
		})
		return nil
	}

	switch p.Packet {
	case model.PktQueryGeneral:
		// gqCount is the zero-based index of THIS delivered query.
		round := r.gqCount
		for _, mem := range r.fabric.Members(p.Iface, r.script.Group) {
			r.schedule(p.At, mem, r.script.Group, p.Gen,
				model.PktQueryGeneral, r.generalDelay(mem.Name, round))
		}
		r.gqCount++
	case model.PktQueryGroup:
		if p.Group != r.script.Group {
			return nil
		}
		for _, mem := range r.fabric.Members(p.Iface, p.Group) {
			r.schedule(p.At, mem, p.Group, p.Gen,
				model.PktQueryGroup, r.lDelay[mem.Name])
		}
	}
	return nil
}

// generalDelay returns the per-round pinned delay, falling back to the last
// entry (or zero) when the fixture declares fewer rounds than were run.
func (r *Runner) generalDelay(member string, round int) int64 {
	ds := r.gDelays[member]
	if len(ds) == 0 {
		return 0
	}
	if round < len(ds) {
		return ds[round]
	}
	return ds[len(ds)-1]
}

func (r *Runner) journalInput(ev model.Event) {
	if r.st != nil {
		_ = r.st.AppendEvent(ev)
	}
}

func (r *Runner) applyScriptEvent(e ScriptEvent, idx int) error {
	iface := e.Iface
	if iface == "" {
		iface = r.script.Iface
	}
	group := e.Group
	if group == "" {
		group = r.script.Group
	}
	reqID := e.RequestID
	if reqID == "" {
		reqID = rid("evt", idx)
	}

	switch e.Kind {
	case "checkpoint":
		r.snaps[e.At] = r.core.Snapshot()
		return nil

	case "advance":
		return nil // clock already advanced to this point by the loop

	case "force_query":
		pkt, d, err := r.core.InjectGeneralQuery(iface, reqID)
		if err != nil {
			if e.Group != "" {
				return fmt.Errorf("force_query with group is not supported: %w", err)
			}
			return err
		}
		r.recordDiag(d)
		// script may force in-transit loss even without a drop rule
		if e.Deliver != nil && !*e.Deliver {
			r.recordDiag(model.Diag{
				Seq: r.nextSeq(), RequestID: reqID, At: model.Millis(e.At),
				Iface: iface, Packet: model.PktQueryGeneral, Verdict: model.VDropped,
				Reason:    model.ReasonQueryDropped,
				Detail:    "general query force-lost by script",
				GenActive: pkt.Gen,
			})
			return nil
		}
		return r.handleEmittedQuery(pkt)

	case "report", "leave":
		h := r.fabric.Host(iface, e.Member)
		if h == nil {
			return fmt.Errorf("event at %d: member %q not declared", e.At, e.Member)
		}
		if e.Kind == "report" {
			h.Join(group)
		} else {
			h.Leave(group)
		}
		ev := model.Event{
			Seq:        r.nextSeq(),
			At:         model.Millis(e.At),
			Iface:      iface,
			Group:      group,
			Member:     e.Member,
			SourceAddr: h.Addr,
			RequestID:  reqID,
		}
		if e.Kind == "report" {
			ev.Kind = model.EvReport
			switch {
			case e.RefGeneral > 0:
				ev.ResponseTo = strconv.FormatInt(int64(e.RefGeneral), 10)
			case e.ResponseTo > 0:
				ev.ResponseTo = strconv.FormatInt(e.ResponseTo, 10)
			default:
				ev.ResponseTo = "" // unsolicited
			}
		} else {
			ev.Kind = model.EvLeave
		}
		d := r.core.Apply(ev)
		if d.Verdict == model.VAccepted {
			// Journal only state-changing inputs; the rebuild re-derives
			// timer state from these, so generation tags are stripped.
			j := ev
			j.ResponseTo = ""
			r.journalInput(j)
		}
		r.recordDiag(d)
		return nil
	}
	return fmt.Errorf("unknown event kind %q", e.Kind)
}

func (r *Runner) emittedPackets() []model.EmittedPkt {
	// collect from diagnostics? core tracks its own emitted list; expose
	// via snapshot of journal when available, else reconstruct from store.
	if r.st != nil {
		pkts, err := r.st.Emitted()
		if err == nil && len(pkts) > 0 {
			return pkts
		}
	}
	// fallback: queries recorded as DROPPED/ACCEPTED diags carry gens;
	// emitted list is also reachable through EmittedPackets().
	return r.core.EmittedPackets()
}

// ---------- assertions ----------

func (r *Runner) groupPresentAt(at model.Millis, iface, group string) (bool, *model.GroupSnapshot) {
	snap, ok := r.history[int64(at)]
	if !ok {
		return false, nil
	}
	for i, g := range snap.Groups {
		if g.Iface == iface && g.Group == group {
			return true, &snap.Groups[i]
		}
	}
	return false, nil
}

func diagMatches(d model.Diag, t ExpectDiag) (bool, string) {
	if t.AtMs != 0 && int64(d.At) != t.AtMs {
		return false, fmt.Sprintf("at_ms %d != %d", d.At, t.AtMs)
	}
	if t.Iface != "" && d.Iface != t.Iface {
		return false, "iface"
	}
	if t.Group != "" && d.Group != t.Group {
		return false, "group"
	}
	if t.Member != "" && d.Member != t.Member {
		return false, "member"
	}
	if t.Verdict != "" && string(d.Verdict) != t.Verdict {
		return false, fmt.Sprintf("verdict %s != %s", d.Verdict, t.Verdict)
	}
	if t.Reason != "" && d.Reason != t.Reason {
		return false, fmt.Sprintf("reason %s != %s", d.Reason, t.Reason)
	}
	if t.Packet != "" && string(d.Packet) != t.Packet {
		return false, "packet"
	}
	if t.RequestID != "" && d.RequestID != t.RequestID {
		return false, "request_id"
	}
	if t.GenActive != 0 && d.GenActive != t.GenActive {
		return false, fmt.Sprintf("gen_active %d != %d", d.GenActive, t.GenActive)
	}
	if t.GenApplied != 0 && d.GenApplied != t.GenApplied {
		return false, fmt.Sprintf("gen_applied %d != %d", d.GenApplied, t.GenApplied)
	}
	if t.DeadlineMs != 0 && int64(d.MembershipDeadline) != t.DeadlineMs {
		return false, fmt.Sprintf("deadline %d != %d", d.MembershipDeadline, t.DeadlineMs)
	}
	if t.Members != nil {
		if len(d.Members) != len(t.Members) {
			return false, fmt.Sprintf("members %v != %v", d.Members, t.Members)
		}
		got := append([]string{}, d.Members...)
		want := append([]string{}, t.Members...)
		sort.Strings(got)
		sort.Strings(want)
		for i := range got {
			if got[i] != want[i] {
				return false, fmt.Sprintf("members %v != %v", got, want)
			}
		}
	}
	return true, ""
}

func (r *Runner) evaluate() {
	for _, a := range r.script.Assertions {
		res := AssertionResult{ID: a.ID, Check: a.Check}
		switch a.Check {
		case CheckPresent:
			if ok, g := r.groupPresentAt(model.Millis(a.AtMs), a.Iface, a.Group); ok {
				res.Pass = true
				res.Detail = fmt.Sprintf("present; deadline=%d members=%v",
					g.MembershipDeadline, g.Members)
			} else {
				res.Failure = FailExpectedPresent
				res.Detail = fmt.Sprintf("group %s on %s absent at %d",
					a.Group, a.Iface, a.AtMs)
			}
		case CheckAbsent:
			if ok, _ := r.groupPresentAt(model.Millis(a.AtMs), a.Iface, a.Group); !ok {
				res.Pass = true
				res.Detail = "absent as expected"
			} else {
				res.Failure = FailExpectedAbsent
				res.Detail = fmt.Sprintf("group %s on %s still present at %d",
					a.Group, a.Iface, a.AtMs)
			}
		case CheckDiag:
			var hits []model.Diag
			var mismatch string
			for _, d := range r.report.Diags {
				ok, why := diagMatches(d, a.Target)
				if ok {
					hits = append(hits, d)
				} else if why != "" && mismatch == "" {
					mismatch = why
				}
			}
			switch len(hits) {
			case 1:
				res.Pass = true
				res.Detail = fmt.Sprintf("matched %s/%s at %d (req=%s)",
					hits[0].Verdict, hits[0].Reason, hits[0].At, hits[0].RequestID)
			case 0:
				res.Failure = FailDiagNotFound
				res.Detail = fmt.Sprintf("no diagnostic matched target (closest mismatch: %s)", mismatch)
			default:
				res.Failure = FailDiagMultiple
				res.Detail = fmt.Sprintf("%d diagnostics matched target", len(hits))
			}
		case CheckCount:
			n := 0
			for _, p := range r.report.Emitted {
				if a.Packet != "" && string(p.Packet) != a.Packet {
					continue
				}
				if a.Iface != "" && p.Iface != a.Iface {
					continue
				}
				if a.Group != "" && p.Group != a.Group {
					continue
				}
				if a.AtMsCount != 0 && int64(p.At) > a.AtMsCount {
					continue
				}
				n++
			}
			// alternatively count diagnostics by verdict (e.g. SUPPRESSED)
			if a.Packet == "" && a.Verdict != "" {
				n = 0
				for _, d := range r.report.Diags {
					if string(d.Verdict) != a.Verdict {
						continue
					}
					if a.AtMsCount != 0 && int64(d.At) > a.AtMsCount {
						continue
					}
					n++
				}
			}
			if n == a.Want {
				res.Pass = true
				res.Detail = fmt.Sprintf("count=%d", n)
			} else {
				res.Failure = FailCountMismatch
				res.Detail = fmt.Sprintf("count=%d want=%d (%s)", n, a.Want, a.Packet)
			}
		case CheckInterval:
			var found *model.Interval
			for i := range r.report.Intervals {
				iv := &r.report.Intervals[i]
				if iv.Iface == a.Iface && iv.Group == a.Group &&
					int64(iv.Start) == a.StartMs {
					found = iv
					break
				}
			}
			if found == nil {
				res.Failure = FailIntervalMismatch
				res.Detail = fmt.Sprintf("no interval starting %d for %s/%s",
					a.StartMs, a.Iface, a.Group)
				break
			}
			if int64(found.End) == a.EndMs {
				res.Pass = true
				res.Detail = fmt.Sprintf("retained %d..%d (%dms) reason=%s",
					found.Start, found.End, found.End-found.Start, found.Reason)
			} else {
				res.Failure = FailIntervalMismatch
				res.Detail = fmt.Sprintf("interval %d..%d, wanted end %d",
					found.Start, found.End, a.EndMs)
			}
		case CheckGeneration:
			var hit *model.Diag
			for i := range r.report.Diags {
				ok, _ := diagMatches(r.report.Diags[i], a.Target)
				if ok {
					hit = &r.report.Diags[i]
					break
				}
			}
			if hit == nil {
				res.Failure = FailDiagNotFound
				res.Detail = "no diagnostic matched generation-guard target"
			} else if hit.GenActive != 0 && hit.GenApplied != 0 && hit.GenActive != hit.GenApplied {
				res.Pass = true
				res.Detail = fmt.Sprintf("old round gen %d did not overwrite active gen %d at %d",
					hit.GenApplied, hit.GenActive, hit.At)
			} else {
				res.Failure = FailGenMismatch
				res.Detail = fmt.Sprintf("gens not distinguished: active=%d applied=%d",
					hit.GenActive, hit.GenApplied)
			}
		}
		r.report.Assertions = append(r.report.Assertions, res)
	}
}

// Trace renders a human-readable diagnostic trace. Source IPs are not part
// of the trace (members are synthetic names); all addresses printed are
// multicast group addresses, which are non-identifying.
func (rep *Report) Trace() string {
	var b strings.Builder
	fmt.Fprintf(&b, "=== scenario %s ===\n", rep.Script)
	for _, d := range rep.Diags {
		fmt.Fprintf(&b, "t=%-7d req=%-12s %-13s if=%s grp=%s member=%-4s %-11s %s",
			d.At, dash(d.RequestID), d.Packet, d.Iface, dash(d.Group), dash(d.Member),
			d.Verdict, d.Reason)
		if d.Detail != "" {
			fmt.Fprintf(&b, " — %s", d.Detail)
		}
		if d.MembershipDeadline != 0 {
			fmt.Fprintf(&b, " [deadline=%d members=%v]", d.MembershipDeadline, d.Members)
		}
		if d.GenActive != 0 || d.GenApplied != 0 {
			fmt.Fprintf(&b, " [gen active=%d applied=%d]", d.GenActive, d.GenApplied)
		}
		b.WriteByte('\n')
	}
	pass, fail := 0, 0
	for _, a := range rep.Assertions {
		if a.Pass {
			pass++
		} else {
			fail++
		}
		mark := "PASS"
		if !a.Pass {
			mark = "FAIL " + a.Failure
		}
		fmt.Fprintf(&b, "ASSERT %-30s %-6s %s\n", a.ID, mark, a.Detail)
	}
	fmt.Fprintf(&b, "--- %d passed, %d failed ---\n", pass, fail)
	return b.String()
}

func dash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}
