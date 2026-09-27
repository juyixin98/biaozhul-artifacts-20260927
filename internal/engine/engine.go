// Package engine implements the path-vector replay: it takes a validated
// topology, per-router import/export policies and an ordered stream of
// synthetic seed UPDATEs, then propagates UPDATEs hop by hop until the
// network quiesces, a deterministic oscillation is proven, or the step
// budget is spent.
//
// Only synthetic SeedEvents are accepted as input; internal Messages are
// engine-private and never deserialized from user data.
package engine

import (
	"fmt"
	"sort"
	"strings"

	"pathvector/internal/config"
	"pathvector/internal/ierr"
	"pathvector/internal/model"
)

// Logger is the minimal logging sink used while a run executes.
type Logger interface {
	Logf(format string, args ...any)
}

// Options controls one run.
type Options struct {
	Budget   int
	QueueCap int
	Logger   Logger
}

// Run executes one replay and returns the full report.
//
// Hard errors (invalid seed input, state conflict, queue-cap exhaustion)
// return a non-nil error carrying an ierr.Kind, together with a partial
// report containing the trace up to the failure. Budget exhaustion and
// proven oscillation are NOT errors: they come back as
// Status "not_converged" with Reason budget_exceeded / oscillation_detected
// and cycle evidence.
func Run(runID string, idx *model.Index, policies map[string]*config.Policy,
	events []model.SeedEvent, opts Options) (*Report, error) {

	e := &engine{
		runID:      runID,
		idx:        idx,
		policies:   policies,
		budget:     opts.Budget,
		queueCap:   opts.QueueCap,
		logger:     opts.Logger,
		totalSeeds: len(events),
		rib:        map[string]map[string]map[string]*model.Candidate{},
		best:       map[string]map[string]*model.Candidate{},
		ribOut:     map[outKey]*model.Candidate{},
		seen:       map[string]int{},
		prefixes:   map[string]struct{}{},
	}

	seeds := sortedSeedCopy(events)
	for i := range seeds {
		if e.step >= e.budget {
			return e.buildReport(StatusNotConverged, ReasonBudgetExceeded), nil
		}
		if err := e.consumeSeed(seeds[i]); err != nil {
			return e.partialReport(err), err
		}
		e.processedSeeds++
		cycled, err := e.drain()
		if err != nil {
			return e.partialReport(err), err
		}
		if cycled {
			return e.buildReport(StatusNotConverged, ReasonOscillation), nil
		}
		if e.step >= e.budget {
			return e.buildReport(StatusNotConverged, ReasonBudgetExceeded), nil
		}
	}
	return e.buildReport(StatusConverged, ReasonQuiesced), nil
}

type engine struct {
	runID    string
	idx      *model.Index
	policies map[string]*config.Policy
	budget   int
	queueCap int
	logger   Logger

	step           int
	totalSeeds     int
	processedSeeds int

	// rib: router -> prefix -> peer -> candidate
	rib map[string]map[string]map[string]*model.Candidate
	// best: router -> prefix -> chosen candidate
	best map[string]map[string]*model.Candidate
	// ribOut: (router, neighbor, prefix) -> last advertised candidate
	ribOut map[outKey]*model.Candidate

	queue    []model.Message
	seen     map[string]int
	walk     []StatePoint
	trace    []TraceEvent
	cycle    *CycleEvidence
	prefixes map[string]struct{}
}

type outKey struct {
	router, neighbor, prefix string
}

func (e *engine) logf(format string, args ...any) {
	if e.logger != nil {
		e.logger.Logf("[run=%s step=%d] %s", e.runID, e.step, fmt.Sprintf(format, args...))
	}
}

// sortedSeedCopy returns events ordered by Seq without mutating caller data.
func sortedSeedCopy(events []model.SeedEvent) []model.SeedEvent {
	out := append([]model.SeedEvent(nil), events...)
	sort.SliceStable(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out
}

// drain processes internal UPDATEs until the queue empties (quiescence), a
// recurring (best-state, queue) signature is observed (cycle), the budget is
// spent, or the queue cap is hit (hard error).
func (e *engine) drain() (cycled bool, err error) {
	for len(e.queue) > 0 {
		if e.step >= e.budget {
			return false, nil
		}
		msg := e.queue[0]
		e.queue = e.queue[1:]
		e.step++
		e.logf("propagate %s %s from %s to %s", msg.Kind, msg.Prefix, msg.From, msg.To)
		switch msg.Kind {
		case model.MsgAnnounce:
			cand, perr := msg.Snap.ToCandidate()
			if perr != nil {
				return false, ierr.Wrap(ierr.KindComputationFailed, "engine.drain",
					"internal message carried invalid origin", perr)
			}
			if perr = e.install(msg.To, msg.From, cand, "propagate"); perr != nil {
				return false, perr
			}
		case model.MsgWithdraw:
			if perr := e.remove(msg.To, msg.From, msg.Prefix, "propagate"); perr != nil {
				return false, perr
			}
		default:
			return false, ierr.New(ierr.KindComputationFailed, "engine.drain",
				"unknown internal message kind "+string(msg.Kind))
		}
		if len(e.queue) > e.queueCap {
			return false, ierr.New(ierr.KindResourceExhausted, "engine.drain",
				"internal message queue cap "+fmt.Sprint(e.queueCap)+" exceeded")
		}
		// A cycle is a deterministic recurrence of the complete future:
		// best-path choices everywhere plus the exact FIFO queue contents.
		sig, point := e.stateSignature()
		if first, ok := e.seen[sig]; ok {
			e.cycle = &CycleEvidence{
				FirstStep:  first,
				SecondStep: point.Step,
				Signature:  sig,
				Walk:       append([]StatePoint(nil), e.walk...),
			}
			return true, nil
		}
		e.seen[sig] = point.Step
		e.walk = append(e.walk, point)
	}
	return false, nil
}

func (e *engine) consumeSeed(ev model.SeedEvent) error {
	e.step++
	e.prefixes[ev.Prefix] = struct{}{}
	e.logf("seed %s seq=%d router=%s prefix=%s peer=%q", ev.Kind, ev.Seq, ev.RouterID, ev.Prefix, ev.Peer)
	switch ev.Kind {
	case model.SeedAnnounce:
		origin, err := model.ParseOrigin(ev.Origin)
		if err != nil {
			return ierr.Wrap(ierr.KindInvalidInput, "engine.announce",
				fmt.Sprintf("seq %d", ev.Seq), err)
		}
		lp := uint32(100)
		if ev.LocalPref != nil {
			lp = *ev.LocalPref
		}
		nh := ev.NextHop
		if ev.Peer != "" && nh == "" {
			nh = e.idx.AddressOf(ev.Peer)
		}
		cand := model.Candidate{
			Prefix:   ev.Prefix,
			FromPeer: ev.Peer, // "" marks local origin
			NextHop:  nh,
			Attrs: model.Attrs{
				LocalPref: lp,
				ASPath:    append([]int(nil), ev.ASPath...),
				MED:       derefU32(ev.MED),
				Origin:    origin,
			},
		}
		if ev.Peer != "" {
			cand.Attrs.LearnedIBGP = e.idx.SameAS(ev.RouterID, ev.Peer)
		}
		return e.install(ev.RouterID, ev.Peer, cand, "seed")
	case model.SeedWithdraw:
		// An empty peer removes the router's own local-origin candidate;
		// neighbor-learned candidates are addressed by peer id and are
		// never touched.
		if !e.hasCandidate(ev.RouterID, ev.Prefix, ev.Peer) {
			e.trace = append(e.trace, TraceEvent{
				Step:    e.step,
				Phase:   "seed",
				Kind:    "withdraw",
				Router:  ev.RouterID,
				From:    ev.Peer,
				Prefix:  ev.Prefix,
				Outcome: "rejected_unknown_withdraw",
				Reason:  ReasonUnknownWithdraw,
			})
			which := ev.Peer
			if which == "" {
				which = "local-origin"
			}
			return ierr.New(ierr.KindStateConflict, "engine.withdraw",
				fmt.Sprintf("seq %d: router %s has no %s candidate for %s; other sources are untouched",
					ev.Seq, ev.RouterID, which, ev.Prefix))
		}
		return e.remove(ev.RouterID, ev.Peer, ev.Prefix, "seed")
	}
	return ierr.New(ierr.KindInvalidInput, "engine.seed", "unknown event kind "+string(ev.Kind))
}

func derefU32(p *uint32) uint32 {
	if p == nil {
		return 0
	}
	return *p
}

func (e *engine) hasCandidate(router, prefix, peer string) bool {
	ps, ok := e.rib[router]
	if !ok {
		return false
	}
	cs, ok := ps[prefix]
	if !ok {
		return false
	}
	_, ok = cs[peer]
	return ok
}

// install applies loop guard and import policy, upserts the candidate,
// recomputes best and propagates. A rejected candidate is treated exactly
// like a withdrawal of that peer's candidate: other peers are untouched.
func (e *engine) install(router, peer string, cand model.Candidate, phase string) error {
	e.prefixes[cand.Prefix] = struct{}{}
	t := TraceEvent{
		Step:   e.step,
		Phase:  phase,
		Kind:   "announce",
		Router: router,
		From:   peer,
		Prefix: cand.Prefix,
	}
	defer func() { e.trace = append(e.trace, t) }()

	// 1) AS_PATH loop guard runs first — before import policy.
	if cand.Attrs.ContainsAS(e.idx.ASN(router)) {
		t.Outcome = "rejected_loop"
		t.Reason = "as_path_contains_local_as"
		t.Candidates = e.snapshotCandidates(router, cand.Prefix)
		e.logf("reject from %s: AS_PATH %v contains local ASN %d", peer, cand.Attrs.ASPath, e.idx.ASN(router))
		return nil
	}

	// 2) Import policy (external candidates only; local origin bypasses).
	if peer != model.LocalOrigin {
		got, decision, permit := config.ApplyImport(e.policies[router], peer, cand)
		t.Rule = decision.Rule
		if !permit {
			e.removeCandidate(router, cand.Prefix, peer)
			t.Outcome = "rejected_import_policy"
			t.Candidates = e.snapshotCandidates(router, cand.Prefix)
			e.recompute(router, cand.Prefix, &t)
			qs, denied, err := e.propagate(router, cand.Prefix)
			if err != nil {
				return err
			}
			t.Queued = qs
			t.DeniedExports = denied
			e.logf("import deny rule=%s from %s for %s", decision.Rule, peer, cand.Prefix)
			return nil
		}
		cand = got
	}

	// 3) Adj-RIB-In upsert keyed by the advertising neighbor only.
	if e.rib[router] == nil {
		e.rib[router] = map[string]map[string]*model.Candidate{}
	}
	if e.rib[router][cand.Prefix] == nil {
		e.rib[router][cand.Prefix] = map[string]*model.Candidate{}
	}
	c := cand
	e.rib[router][cand.Prefix][peer] = &c

	t.Outcome = "accepted"
	t.Candidates = e.snapshotCandidates(router, cand.Prefix)
	e.recompute(router, cand.Prefix, &t)
	qs, denied, err := e.propagate(router, cand.Prefix)
	if err != nil {
		return err
	}
	t.Queued = qs
	t.DeniedExports = denied
	return nil
}

// remove deletes exactly one peer's candidate and re-runs selection. It
// never touches candidates from other neighbors.
func (e *engine) remove(router, peer, prefix, phase string) error {
	t := TraceEvent{
		Step:   e.step,
		Phase:  phase,
		Kind:   "withdraw",
		Router: router,
		From:   peer,
		Prefix: prefix,
	}
	defer func() { e.trace = append(e.trace, t) }()
	if !e.removeCandidate(router, prefix, peer) {
		t.Outcome = "withdraw_redundant"
		t.Reason = "no_such_candidate"
		return nil
	}
	t.Outcome = "withdrawn"
	t.Candidates = e.snapshotCandidates(router, prefix)
	e.recompute(router, prefix, &t)
	qs, denied, err := e.propagate(router, prefix)
	if err != nil {
		return err
	}
	t.Queued = qs
	t.DeniedExports = denied
	return nil
}

func (e *engine) removeCandidate(router, prefix, peer string) bool {
	ps, ok := e.rib[router]
	if !ok {
		return false
	}
	cs, ok := ps[prefix]
	if !ok {
		return false
	}
	if _, ok := cs[peer]; !ok {
		return false
	}
	delete(cs, peer)
	return true
}

// recompute runs the comparator over all candidates for one prefix.
func (e *engine) recompute(router, prefix string, t *TraceEvent) {
	if e.best[router] == nil {
		e.best[router] = map[string]*model.Candidate{}
	}
	var cands []*model.Candidate
	for _, c := range e.rib[router][prefix] {
		cands = append(cands, c)
	}
	// Deterministic order into the comparator (peer ordinal).
	sort.Slice(cands, func(i, j int) bool {
		return e.idx.Ordinal(cands[i].FromPeer) < e.idx.Ordinal(cands[j].FromPeer)
	})
	if len(cands) == 0 {
		e.best[router][prefix] = nil
		return
	}
	ctx := cmpCtx{idx: e.idx, receiver: router}
	b, reason := selectBest(cands, ctx)
	e.best[router][prefix] = b
	snap := snapshotCandidate(b)
	t.Best = &snap
	t.Reason = reason
}

// propagate emits the diff between the previous Adj-RIB-Out per neighbor
// and the export-filtered view of the current best path. It returns the
// messages enqueued and the export-policy denials so the caller can attach
// both to its trace event.
func (e *engine) propagate(router, prefix string) ([]MessageSnap, []string, error) {
	best := e.best[router][prefix]
	var msgs []model.Message
	var denied []string
	for _, nbr := range e.idx.Neighbors(router) {
		key := outKey{router, nbr, prefix}
		prev := e.ribOut[key]

		if best == nil {
			if prev != nil {
				msgs = append(msgs, model.Message{Kind: model.MsgWithdraw, Prefix: prefix, From: router, To: nbr})
				delete(e.ribOut, key)
			}
			continue
		}

		// Split horizon: never readvertise a route back to the neighbor
		// it was learned from (eBGP and iBGP alike).
		if best.FromPeer == nbr {
			continue
		}
		// iBGP split horizon: an iBGP-learned route is never forwarded
		// to another iBGP peer.
		eBGP := !e.idx.SameAS(router, nbr)
		if best.Attrs.LearnedIBGP && !eBGP {
			if prev != nil {
				msgs = append(msgs, model.Message{Kind: model.MsgWithdraw, Prefix: prefix, From: router, To: nbr})
				delete(e.ribOut, key)
			}
			continue
		}

		out, act, permit := config.ApplyExport(e.policies[router], nbr, *best)
		if !permit {
			denied = append(denied, nbr+":"+act.Decision.Rule)
			if prev != nil {
				msgs = append(msgs, model.Message{Kind: model.MsgWithdraw, Prefix: prefix, From: router, To: nbr})
				delete(e.ribOut, key)
			}
			e.logf("export deny rule=%s to %s for %s", act.Decision.Rule, nbr, prefix)
			continue
		}

		// Build outbound attributes.
		if eBGP {
			own := e.idx.ASN(router)
			prepends := 1 + act.PrependCount // mandatory eBGP prepend + policy
			path := make([]int, 0, len(out.Attrs.ASPath)+prepends)
			for i := 0; i < prepends; i++ {
				path = append(path, own)
			}
			path = append(path, out.Attrs.ASPath...)
			out.Attrs.ASPath = path
			if addr := e.idx.AddressOf(router); addr != "" {
				out.NextHop = addr
			}
			out.Attrs.LearnedIBGP = false
		} else {
			// iBGP: AS_PATH is preserved, the route is marked iBGP-learned
			// at the receiver, and next-hop-self is applied so the IGP
			// early-exit comparison resolves to the egress border router.
			if addr := e.idx.AddressOf(router); addr != "" {
				out.NextHop = addr
			}
			out.Attrs.LearnedIBGP = true
		}
		out.FromPeer = router

		if prev != nil && sameExport(*prev, out) {
			continue // no UPDATE on the wire
		}
		sent := out
		e.ribOut[key] = &sent
		msgs = append(msgs, model.Message{
			Kind:   model.MsgAnnounce,
			Prefix: prefix,
			From:   router,
			To:     nbr,
			Snap:   out.Snapshot(),
		})
	}
	snaps := make([]MessageSnap, 0, len(msgs))
	for _, m := range msgs {
		if err := e.enqueue(m); err != nil {
			return nil, nil, err
		}
		snap := MessageSnap{
			Kind: string(m.Kind), Prefix: m.Prefix, From: m.From, To: m.To,
		}
		if m.Kind == model.MsgAnnounce {
			snap.LocalPref = m.Snap.LocalPref
			snap.MED = m.Snap.MED
			snap.ASPath = append([]int(nil), m.Snap.ASPath...)
		}
		snaps = append(snaps, snap)
	}
	return snaps, denied, nil
}

func (e *engine) enqueue(m model.Message) error {
	if len(e.queue) >= e.queueCap {
		return ierr.New(ierr.KindResourceExhausted, "engine.enqueue",
			"internal message queue cap "+fmt.Sprint(e.queueCap)+" exceeded")
	}
	e.queue = append(e.queue, m)
	return nil
}

func sameExport(a, b model.Candidate) bool {
	return a.NextHop == b.NextHop &&
		a.Attrs.LocalPref == b.Attrs.LocalPref &&
		a.Attrs.MED == b.Attrs.MED &&
		a.Attrs.Origin == b.Attrs.Origin &&
		a.Attrs.LearnedIBGP == b.Attrs.LearnedIBGP &&
		eqInts(a.Attrs.ASPath, b.Attrs.ASPath)
}

func eqInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// stateSignature hashes (best-path choices, queued message sequence). The
// pair is a deterministic-future certificate: in this replay the only
// inputs are the FIFO queue and per-prefix best choices, so an identical
// pair recurs only on a genuine cycle.
func (e *engine) stateSignature() (string, StatePoint) {
	var b strings.Builder
	pfxs := keysOf(e.prefixes)
	sort.Strings(pfxs)
	for _, p := range pfxs {
		for _, r := range e.idx.Order() {
			c := e.best[r][p]
			if c == nil {
				continue
			}
			b.WriteString(r)
			b.WriteString("|")
			b.WriteString(p)
			b.WriteString("=")
			b.WriteString(c.FromPeer)
			fmt.Fprintf(&b, ":lp%d:path", c.Attrs.LocalPref)
			for _, x := range c.Attrs.ASPath {
				fmt.Fprintf(&b, "/%d", x)
			}
			b.WriteString(";")
		}
	}
	b.WriteString("#queue:")
	for _, m := range e.queue {
		fmt.Fprintf(&b, "%s>%s:%s/%s;", m.From, m.To, m.Kind, m.Prefix)
	}
	point := StatePoint{Step: e.step, Signature: b.String()}
	return b.String(), point
}

func keysOf(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func snapshotCandidate(c *model.Candidate) CandidateSnap {
	if c == nil {
		return CandidateSnap{}
	}
	return CandidateSnap{
		FromPeer:    c.FromPeer,
		NextHop:     c.NextHop,
		LocalPref:   c.Attrs.LocalPref,
		ASPath:      append([]int(nil), c.Attrs.ASPath...),
		MED:         c.Attrs.MED,
		Origin:      c.Attrs.Origin.String(),
		LearnedIBGP: c.Attrs.LearnedIBGP,
	}
}

func (e *engine) snapshotCandidates(router, prefix string) []CandidateSnap {
	cs := e.rib[router][prefix]
	out := make([]CandidateSnap, 0, len(cs))
	ids := make([]string, 0, len(cs))
	for id := range cs {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return e.idx.Ordinal(ids[i]) < e.idx.Ordinal(ids[j]) })
	for _, id := range ids {
		out = append(out, snapshotCandidate(cs[id]))
	}
	return out
}
