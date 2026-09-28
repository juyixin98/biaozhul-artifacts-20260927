package engine

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"

	"pvsim/config"
	"pvsim/model"
)

// lastSent records what a router last advertised to a peer for a prefix,
// so export only emits deltas. nil attrs == "withdrawn / nothing".
type lastSentKey struct {
	from, to string
	prefix   model.Prefix
}

type ribKey struct {
	router string
	prefix model.Prefix
}

// engine is the mutable replay state.
type engine struct {
	sc   *config.Scenario
	opt  Options
	sink Sink

	version int // increments on every delivered message
	steps   int // messages processed

	// RIB: per router, per prefix: candidates keyed by direct peer.
	cand map[ribKey]map[string]*Candidate
	// Current best peer per (router,prefix); "" = no route.
	best map[ribKey]string
	// last decided reason per (router,prefix).
	reason map[ribKey]string
	// FIFO message queue (internal propagation + one injected event at a time).
	queue []*Message
	// last advertised state per directed edge+prefix.
	sent map[lastSentKey]*model.Attrs

	// Signature index for oscillation detection: signature -> first version.
	sigSeen map[string]int
	// Snapshots retained when a state first appears, for cycle evidence.
	snapSeen map[string]ChoiceSnapshot

	// external events are injected lazily: the next event is only
	// injected once the internal queue has drained, so the network
	// reaches quiescence between successive synthetic occurrences.
	extIdx int
}

// Run executes a scenario to convergence or to the step budget.
func Run(sc *config.Scenario, opt Options, sink Sink) (*Result, error) {
	max := opt.MaxSteps
	if max <= 0 {
		max = sc.MaxSteps
	}
	if max <= 0 {
		max = DefaultMaxSteps
	}
	if max > HardMaxSteps {
		return nil, model.NewError(model.KindResourceExhausted, "MAX_STEPS_CAP",
			"max_steps %d exceeds hard cap %d", max, HardMaxSteps)
	}
	if sink == nil {
		sink = nopSink{}
	}
	e := &engine{
		sc:       sc,
		opt:      opt,
		sink:     sink,
		cand:     map[ribKey]map[string]*Candidate{},
		best:     map[ribKey]string{},
		reason:   map[ribKey]string{},
		sent:     map[lastSentKey]*model.Attrs{},
		sigSeen:  map[string]int{},
		snapSeen: map[string]ChoiceSnapshot{},
	}

	var cycle *CycleEvidence
	for {
		// Inject the next synthetic occurrence once the network is quiet.
		if len(e.queue) == 0 {
			if e.extIdx >= len(sc.Events) {
				break
			}
			ev := sc.Events[e.extIdx]
			e.extIdx++
			m := &Message{
				From: ev.Peer, To: ev.Router, Prefix: model.Prefix(ev.Prefix),
				External: true, EventSeq: ev.Seq,
			}
			if ev.Kind == "update" {
				m.Kind = MsgUpdate
				m.Attrs = ev.Attrs.Clone()
			} else {
				m.Kind = MsgWithdraw
			}
			e.queue = append(e.queue, m)
		}
		if e.steps >= max {
			res := e.buildResult()
			if cycle != nil {
				res.Converged = false
				res.NonConvergentCode = model.NonConvOscillationBudget
				res.Cycle = cycle
			} else {
				res.Converged = false
				res.NonConvergentCode = model.NonConvBudgetNoCycle
			}
			return res, nil
		}
		m := e.queue[0]
		e.queue = e.queue[1:]
		e.version++
		if m.External {
			var ev *config.Event
			for _, x := range sc.Events {
				if x.Seq == m.EventSeq {
					ev = x
					break
				}
			}
			if ev == nil {
				return nil, model.NewError(model.KindComputeFailed, "EVENT_LOOKUP",
					"internal: external message seq=%d has no event", m.EventSeq)
			}
			e.sink.ExternalDelivered(e.version, ev)
			e.trace(Trace{
				Version: e.version, Category: TraceExternalEvent,
				Router: ev.Router, Peer: ev.Peer, Prefix: ev.Prefix,
				Detail: detail("synthetic event #%d injected: %s from %s%s",
					ev.Seq, ev.Kind, ev.Peer,
					func() string {
						if ev.Kind == "update" {
							return detail(" (path=%v lp=%d med=%d)",
								ev.Attrs.ASPath, ev.Attrs.LocalPrefOr(100), ev.Attrs.MedOr())
						}
						return ""
					}()),
			})
		}
		e.deliver(m)
		e.steps++

		// Convergence has priority over cycle detection: once the queue
		// drained after the final external event, the state is a fixed
		// point even if that signature was seen mid-convergence.
		if len(e.queue) == 0 && e.extIdx >= len(sc.Events) {
			break
		}
		// Oscillation signatures are only meaningful while there is still
		// work in flight AND the external input stream is fully injected.
		if e.extIdx >= len(sc.Events) {
			sig, snap := e.signature()
			if first, ok := e.sigSeen[sig]; ok {
				cycle = &CycleEvidence{
					EntranceVersion:   first,
					RepetitionVersion: e.version,
					Length:            e.version - first,
					Signature:         sig,
				}
				cycle.Choices = []ChoiceSnapshot{e.snapSeen[sig], snap}
			} else {
				e.sigSeen[sig] = e.version
				e.snapSeen[sig] = snap
			}
		}
	}

	res := e.buildResult()
	res.Converged = true
	if cycle != nil {
		// A proven cycle cannot have drained the queue; keep this branch
		// defensive.
		res.Converged = false
		res.NonConvergentCode = model.NonConvOscillationBudget
		res.Cycle = cycle
	}
	return res, nil
}

// externalDrained is retained for documentation of the signature policy;
// the run loop checks extIdx directly.
func (e *engine) externalDrained() bool { return e.extIdx >= len(e.sc.Events) }

// cycleIfAny is retained as a no-op placeholder for call-site readability.
func (e *engine) cycleIfAny() *CycleEvidence { return nil }

// deliver processes one inbound message at m.To.
func (e *engine) deliver(m *Message) {
	local, ok := e.sc.LookupRouter(m.To)
	if !ok {
		// Validated at parse time; invariant.
		panic(fmt.Sprintf("engine: unknown router %q", m.To))
	}
	k := ribKey{router: m.To, prefix: m.Prefix}

	if m.Kind == MsgWithdraw {
		e.handleWithdraw(m, k)
	} else {
		e.handleUpdate(m, local, k)
	}
}

func (e *engine) handleWithdraw(m *Message, k ribKey) {
	bucket := e.cand[k]
	old := bucket[m.From]
	if old == nil {
		// Withdrawal for a route we never held: harmless real-BGP behavior;
		// record it as a no-op trace but do not touch other sources.
		e.trace(Trace{
			Version: e.version, Category: "withdraw_unknown",
			Router: m.To, Peer: m.From, Prefix: string(m.Prefix),
			Detail: "withdrawal ignored: no candidate from this neighbor",
		})
		return
	}
	delete(bucket, m.From)
	if len(bucket) == 0 {
		delete(e.cand, k)
	}
	e.trace(Trace{
		Version: e.version, Category: TraceCandidateRemoved,
		Router: m.To, Peer: m.From, Prefix: string(m.Prefix),
		Detail: detail("withdrawal: candidate from %s removed; %d other source(s) retained",
			m.From, len(bucket)),
		AttrsBefore: ptrAttrs(old.Attrs),
	})
	// Re-run best selection ONLY if the withdrawn source was the best.
	if e.best[k] == m.From {
		e.rebest(k, nil)
	}
}

func (e *engine) handleUpdate(m *Message, local *config.Router, k ribKey) {
	sess, igpCost, ok := e.sc.SessionBetween(m.To, m.From)
	if !ok {
		panic(fmt.Sprintf("engine: no session between %s and %s", m.To, m.From))
	}
	peer, _ := e.sc.LookupRouter(m.From)

	// 1. Inbound AS_PATH loop suppression: path carrying our own AS.
	if m.Attrs.ContainsAS(local.ASN) {
		e.trace(Trace{
			Version: e.version, Category: TraceLoopRejected,
			Router: m.To, Peer: m.From, Prefix: string(m.Prefix),
			Detail: detail("UPDATE rejected: AS_PATH %v contains local AS%d",
				m.Attrs.ASPath, local.ASN),
			AttrsBefore: ptrAttrs(m.Attrs),
		})
		// A looped route is not a valid replacement for anything the peer
		// previously advertised: real speakers hold the stale entry when a
		// received route fails sanity. But a *policy* denial on a fresh
		// UPDATE means that peer no longer offers the previous route,
		// because the speaker can't know whether the replacement or the
		// withdrawal arrived first. To keep semantics explicit and
		// deterministic, loop rejection preserves the stale candidate
		// (sanity failure), while import denial below removes it.
		return
	}

	// 2. Normalize carried attributes BEFORE policy evaluation:
	//    local_pref is iBGP-only; an eBGP UPDATE must not influence us
	//    with a carried local_pref (import policy may still SET one).
	inAttrs := m.Attrs.Clone()
	if sess.Type == "ebgp" {
		inAttrs.LocalPref = nil
	}

	// 3. Import policy at the receiving end.
	rules := e.sc.ImportPolicy(m.To, sess)
	pr := config.EvalPolicy(rules, config.PolicyInput{
		Prefix: m.Prefix, Attrs: inAttrs, FromRouter: m.From,
	})
	if !pr.Permitted {
		e.trace(Trace{
			Version: e.version, Category: TraceImportDenied,
			Router: m.To, Peer: m.From, Prefix: string(m.Prefix),
			Detail: detail("UPDATE denied by import rule %q; stale candidate from %s invalidated",
				ruleName(pr.RuleName), m.From),
			AttrsBefore: ptrAttrs(inAttrs),
		})
		bucket := e.cand[k]
		if bucket != nil {
			old := bucket[m.From]
			delete(bucket, m.From)
			if len(bucket) == 0 {
				delete(e.cand, k)
			}
			if old != nil {
				e.trace(Trace{
					Version: e.version, Category: TraceCandidateRemoved,
					Router: m.To, Peer: m.From, Prefix: string(m.Prefix),
					Detail: detail("stale candidate removed after import denial; %d other source(s) retained",
						len(bucket)),
					AttrsBefore: ptrAttrs(old.Attrs),
				})
			}
			if e.best[k] == m.From {
				e.rebest(k, nil)
			}
		}
		return
	}
	attrs := pr.Attrs

	// 4. Default local_pref when nothing (event nor policy) provided one.
	//    iBGP routes always carry local_pref; for eBGP routes the import
	//    policy may define one, otherwise the standard default 100.
	if attrs.LocalPref == nil {
		lp := 100
		attrs.LocalPref = &lp
	}

	// 4. Install/replace the candidate from THIS peer only.
	if e.cand[k] == nil {
		e.cand[k] = map[string]*Candidate{}
	}
	old := e.cand[k][m.From]
	c := &Candidate{
		Prefix: m.Prefix, Peer: m.From, SessionID: sess.ID,
		SessionType: sess.Type, Attrs: attrs,
		IGPCost:         igpCost,
		PeerOrdinal:     peer.Ordinal,
		ReceivedVersion: e.version,
	}
	e.cand[k][m.From] = c
	cat := TraceCandidateUpdate
	if old != nil {
		cat = TraceCandidateUpdate
	}
	e.trace(Trace{
		Version: e.version, Category: cat,
		Router: m.To, Peer: m.From, Prefix: string(m.Prefix),
		Detail: detail("candidate installed from %s (%s, path=%v, lp=%d, med=%d)%s",
			m.From, sess.Type, attrs.ASPath, attrs.LocalPrefOr(100),
			attrs.MedOr(), ruleSuffix(pr.RuleName)),
		AttrsBefore: maybeAttrs(old),
		AttrsAfter:  ptrAttrs(attrs),
	})

	// 5. Re-select best and, if it changed, propagate.
	e.rebest(k, old)
}

// rebest recomputes the best route for one RIB entry and drives export.
// replacedOld is the previous candidate object from the triggering peer
// (nil on withdrawal/no prior candidate), used for decision narration.
func (e *engine) rebest(k ribKey, replacedOld *Candidate) {
	bucket := e.cand[k]
	var chosen *Candidate
	var runnerUp *Candidate
	// Deterministic iteration: peers in declaration order.
	routers := e.sc.Routers
	for _, r := range routers {
		c := bucket[r.Name]
		if c == nil {
			continue
		}
		if chosen == nil {
			chosen = c
			continue
		}
		cmp, _ := model.Compare(chosen.Attrs, c.Attrs, e.cmpInput(chosen), e.cmpInput(c))
		if cmp > 0 { // c is better
			runnerUp = chosen
			chosen = c
		} else {
			if runnerUp == nil || better(c, runnerUp, e) {
				runnerUp = c
			}
		}
	}
	prevPeer := e.best[k]
	var prevAttrs model.Attrs
	if prevPeer != "" && bucket[prevPeer] != nil {
		prevAttrs = bucket[prevPeer].Attrs
	}
	_ = prevAttrs

	if chosen == nil {
		if prevPeer != "" {
			e.sink.Decision(Decision{
				Version: e.version, Router: k.router, Prefix: string(k.prefix),
				PreviousPeer: prevPeer, ChosenPeer: "",
				Reason: "no_candidate",
			})
			e.trace(Trace{
				Version: e.version, Category: TraceBestChanged,
				Router: k.router, Prefix: string(k.prefix), Peer: prevPeer,
				Detail: "best route lost (no candidates remain)",
			})
		}
		delete(e.best, k)
		delete(e.reason, k)
		e.exportAll(k, nil)
		return
	}

	var reason model.Reason
	switch {
	case prevPeer == "" && len(bucket) == 1:
		reason = "only_candidate"
	case prevPeer == "":
		// First selection among several: reason is the step separating the
		// winner from the best of the rest.
		ru := runnerUpOrSecond(bucket, chosen, e)
		_, reason = model.Compare(chosen.Attrs, ru.Attrs,
			e.cmpInput(chosen), e.cmpInput(ru))
	case prevPeer != chosen.Peer && bucket[prevPeer] != nil:
		// Both old and new best are present: the step separating them.
		_, reason = compareAgainstPrev(prevPeer, bucket, chosen, e)
	case prevPeer != chosen.Peer:
		// The previous best left the RIB (withdrawn / invalidated), so it
		// cannot be compared: the reason is why the winner beats the best
		// of the REMAINING candidates (or it is the only one left).
		if len(bucket) == 1 {
			reason = "only_candidate"
		} else {
			ru := runnerUpOrSecond(bucket, chosen, e)
			_, reason = model.Compare(chosen.Attrs, ru.Attrs,
				e.cmpInput(chosen), e.cmpInput(ru))
		}
	default:
		// Winner unchanged; keep the recorded deciding reason.
		reason = model.Reason(e.reason[k])
	}
	e.reason[k] = string(reason)

	if prevPeer != chosen.Peer {
		e.best[k] = chosen.Peer
		e.sink.Decision(Decision{
			Version: e.version, Router: k.router, Prefix: string(k.prefix),
			PreviousPeer: prevPeer, ChosenPeer: chosen.Peer,
			ChosenAttrs:  chosen.Attrs.Clone(),
			RunnerUpPeer: peerName(runnerUp),
			Reason:       string(reason),
		})
		e.trace(Trace{
			Version: e.version, Category: TraceBestChanged,
			Router: k.router, Prefix: string(k.prefix), Peer: chosen.Peer,
			Detail: detail("best %s -> %s (path=%v, lp=%d, med=%d); runner-up=%s; reason=%s",
				prevOrNone(prevPeer), chosen.Peer, chosen.Attrs.ASPath,
				chosen.Attrs.LocalPrefOr(100), chosen.Attrs.MedOr(),
				peerOrNone(runnerUp), reason),
			AttrsBefore: prevAttrsOrNil(k, bucket, prevPeer),
			AttrsAfter:  ptrAttrs(chosen.Attrs),
		})
		e.exportAll(k, chosen)
		return
	}

	// Same winning peer, but its attributes may have changed -> propagate.
	if replacedOld == nil || !attrsEqual(replacedOld.Attrs, chosen.Attrs) {
		e.exportAll(k, chosen)
	}
}

func compareAgainstPrev(prevPeer string, bucket map[string]*Candidate, chosen *Candidate, e *engine) (int, model.Reason) {
	prev := bucket[prevPeer]
	if prev == nil {
		return 0, model.ReasonRouterID
	}
	return model.Compare(prev.Attrs, chosen.Attrs, e.cmpInput(prev), e.cmpInput(chosen))
}

func runnerUpOrSecond(bucket map[string]*Candidate, chosen *Candidate, e *engine) *Candidate {
	for _, r := range e.sc.Routers {
		if c := bucket[r.Name]; c != nil && c.Peer != chosen.Peer {
			return c
		}
	}
	return chosen
}

func better(a, b *Candidate, e *engine) bool {
	cmp, _ := model.Compare(a.Attrs, b.Attrs, e.cmpInput(a), e.cmpInput(b))
	return cmp < 0
}

func (e *engine) cmpInput(c *Candidate) model.CompareInput {
	return model.CompareInput{
		SessionType:     c.SessionType,
		IGPCost:         c.IGPCost,
		PeerOrdinal:     c.PeerOrdinal,
		ReceivedVersion: c.ReceivedVersion,
	}
}

// exportAll computes, for every neighbor of k.router, what that neighbor
// should now receive (UPDATE/WITHDRAW/nothing) and enqueues deltas.
func (e *engine) exportAll(k ribKey, chosen *Candidate) {
	local, _ := e.sc.LookupRouter(k.router)
	// Iterate sessions deterministically by session declaration order;
	// for the router's two ends keep neighbor router order.
	for _, sess := range e.sc.Sessions {
		var peerName string
		switch {
		case sess.A == k.router:
			peerName = sess.B
		case sess.B == k.router:
			peerName = sess.A
		default:
			continue
		}

		lsKey := lastSentKey{from: k.router, to: peerName, prefix: k.prefix}
		prev := e.sent[lsKey]

		// Split-horizon is an iBGP rule: an iBGP-learned route must not be
		// readvertised to the iBGP peer it came from (iBGP peers are
		// assumed full-mesh/route-reflection-free here). eBGP instead relies
		// on AS_PATH loop detection at the receiver, so the message IS sent
		// (with the local AS prepended) and dropped inbound if it loops.
		if chosen != nil && chosen.Peer == peerName && sess.Type == "ibgp" {
			// If we previously advertised something there, withdraw it.
			if prev != nil {
				e.enqueueWithdraw(k.router, peerName, k.prefix)
				e.sent[lsKey] = nil
			}
			e.trace(Trace{
				Version: e.version, Category: TraceSuppressRedisc,
				Router: k.router, Peer: peerName, Prefix: string(k.prefix),
				Detail: detail("iBGP split-horizon: suppressed export toward route source"),
			})
			continue
		}

		if chosen == nil {
			if prev != nil {
				e.enqueueWithdraw(k.router, peerName, k.prefix)
				e.sent[lsKey] = nil
			}
			continue
		}

		// Export policy at the sending end.
		rules := e.sc.ExportPolicy(k.router, sess)
		pr := config.EvalPolicy(rules, config.PolicyInput{
			Prefix: k.prefix, Attrs: chosen.Attrs, FromRouter: peerName,
		})
		if !pr.Permitted {
			e.trace(Trace{
				Version: e.version, Category: TraceExportDenied,
				Router: k.router, Peer: peerName, Prefix: string(k.prefix),
				Detail:      detail("export denied by rule %q toward %s", ruleName(pr.RuleName), peerName),
				AttrsBefore: ptrAttrs(chosen.Attrs),
			})
			if prev != nil {
				e.enqueueWithdraw(k.router, peerName, k.prefix)
				e.sent[lsKey] = nil
			}
			continue
		}
		out := pr.Attrs

		// Protocol export semantics.
		if sess.Type == "ebgp" {
			// local_pref does not leave the AS.
			out.LocalPref = nil
			// Prepend local AS (most recent first). We do NOT suppress the
			// message when the policy-rewritten path already contains the
			// target AS: RFC behavior is to send it and let the receiver's
			// inbound loop check drop it. Suppressing at the sender would
			// hide inbound loop rejection behind an optimization and would
			// break legitimate prepend fixtures.
			out.ASPath = append([]uint32{local.ASN}, out.ASPath...)
		}

		// Emit only deltas.
		if prev != nil && attrsEqual(*prev, out) {
			continue
		}
		m := &Message{
			From: k.router, To: peerName, Prefix: k.prefix,
			Kind: MsgUpdate, Attrs: out.Clone(),
		}
		e.queue = append(e.queue, m)
		if prev == nil {
			e.sent[lsKey] = out.ClonePtr()
		} else {
			e.sent[lsKey] = out.ClonePtr()
		}
		e.trace(Trace{
			Version: e.version, Category: TracePropagateUpdate,
			Router: k.router, Peer: peerName, Prefix: string(k.prefix),
			Detail: detail("UPDATE propagated path=%v lp=%d med=%d origin=%s -> %s%s",
				out.ASPath, out.LocalPrefOr(100), out.MedOr(), out.Origin,
				peerName, ruleSuffix(pr.RuleName)),
			AttrsAfter: ptrAttrs(out),
		})
	}
}

func (e *engine) enqueueWithdraw(from, to string, p model.Prefix) {
	e.queue = append(e.queue, &Message{
		From: from, To: to, Prefix: p, Kind: MsgWithdraw,
	})
	e.trace(Trace{
		Version: e.version, Category: TracePropagateWithdraw,
		Router: from, Peer: to, Prefix: string(p),
		Detail: detail("WITHDRAWAL propagated -> %s", to),
	})
}

// ---------------------------------------------------------------------------
// Signatures / result assembly
// ---------------------------------------------------------------------------

func (e *engine) signature() (string, ChoiceSnapshot) {
	var b strings.Builder
	snap := ChoiceSnapshot{Version: e.version, Best: map[string]map[string]PeerPath{}}

	// 1. Full Adj-RIB-In candidate state (every peer, every prefix). This
	//    MUST be in the signature: during propagation, two moments can
	//    share the same best choices while their RIB-In differs, and only
	//    the full deterministic state recurring proves a true cycle.
	ribKeys := make([]ribKey, 0, len(e.cand))
	for k := range e.cand {
		ribKeys = append(ribKeys, k)
	}
	sort.Slice(ribKeys, func(i, j int) bool {
		if ribKeys[i].router != ribKeys[j].router {
			return ribKeys[i].router < ribKeys[j].router
		}
		return ribKeys[i].prefix < ribKeys[j].prefix
	})
	for _, k := range ribKeys {
		bucket := e.cand[k]
		peers := make([]string, 0, len(bucket))
		for p := range bucket {
			peers = append(peers, p)
		}
		sort.Strings(peers)
		for _, p := range peers {
			c := bucket[p]
			fmt.Fprintf(&b, "rib:%s|%s|%s|%v|%d|%d|%d|%s;",
				k.router, k.prefix, p, c.Attrs.ASPath,
				c.Attrs.LocalPrefOr(100), c.Attrs.MedOr(), c.Attrs.Origin,
				c.SessionType)
		}
	}

	// 2. Best choice + reason per router/prefix.
	routers := append([]*config.Router(nil), e.sc.Routers...)
	for _, r := range routers {
		prefixes := make([]string, 0)
		for k := range e.best {
			if k.router == r.Name {
				prefixes = append(prefixes, string(k.prefix))
			}
		}
		sort.Strings(prefixes)
		m := map[string]PeerPath{}
		for _, p := range prefixes {
			kk := ribKey{router: r.Name, prefix: model.Prefix(p)}
			peer := e.best[kk]
			view := PeerPath{Peer: ""}
			if peer != "" {
				if c := e.cand[kk][peer]; c != nil {
					view = PeerPath{Peer: peer, ASPath: append([]uint32(nil), c.Attrs.ASPath...)}
				} else {
					view = PeerPath{Peer: peer}
				}
			}
			m[p] = view
			fmt.Fprintf(&b, "best:%s|%s|%s|%v;", r.Name, p, view.Peer, view.ASPath)
		}
		snap.Best[r.Name] = m
	}

	// 3. Last-advertised state per directed edge.
	edgeKeys := make([]lastSentKey, 0, len(e.sent))
	for k := range e.sent {
		edgeKeys = append(edgeKeys, k)
	}
	sort.Slice(edgeKeys, func(i, j int) bool {
		if edgeKeys[i].from != edgeKeys[j].from {
			return edgeKeys[i].from < edgeKeys[j].from
		}
		if edgeKeys[i].to != edgeKeys[j].to {
			return edgeKeys[i].to < edgeKeys[j].to
		}
		return edgeKeys[i].prefix < edgeKeys[j].prefix
	})
	for _, k := range edgeKeys {
		v := e.sent[k]
		if v == nil {
			fmt.Fprintf(&b, "adv:%s>%s|%s=w;", k.from, k.to, k.prefix)
		} else {
			fmt.Fprintf(&b, "adv:%s>%s|%s=%v|%d|%d|%d;", k.from, k.to, k.prefix,
				v.ASPath, v.LocalPrefOr(100), v.MedOr(), v.Origin)
		}
	}

	// 4. Pending FIFO queue in order — required so an in-flight transient
	//    cannot hash equal to a settled state.
	for _, m := range e.queue {
		fmt.Fprintf(&b, "q:%s>%s|%s|%s|%v;", m.From, m.To, m.Prefix, m.Kind, m.Attrs.ASPath)
	}

	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:8]), snap
}

func (e *engine) buildResult() *Result {
	res := &Result{
		Steps:    e.steps,
		Versions: e.version,
		Best:     map[string][]BestView{},
	}
	for _, r := range e.sc.Routers {
		var views []BestView
		prefixes := map[model.Prefix]bool{}
		for k := range e.best {
			if k.router == r.Name {
				prefixes[k.prefix] = true
			}
		}
		ordered := make([]string, 0, len(prefixes))
		for p := range prefixes {
			ordered = append(ordered, string(p))
		}
		sort.Strings(ordered)
		for _, p := range ordered {
			k := ribKey{router: r.Name, prefix: model.Prefix(p)}
			peer := e.best[k]
			if peer == "" {
				continue
			}
			c := e.cand[k][peer]
			if c == nil {
				continue
			}
			views = append(views, BestView{
				Prefix: p, Peer: peer, SessionType: c.SessionType,
				Attrs: c.Attrs.Clone(), Reason: e.reason[k], Version: c.ReceivedVersion,
			})
		}
		res.Best[r.Name] = views
	}
	return res
}

func (e *engine) trace(t Trace) { e.sink.Trace(t) }
