// Package oracle is an INDEPENDENT reference solver used by tests to
// verify the engine's results. It deliberately does NOT import engine and
// uses a different algorithm:
//
//   - engine: asynchronous FIFO event-driven SPVP replay (one delivery
//     per step, immediate re-best, delta exports);
//   - oracle: synchronous rounds (every router recomputes best routes from
//     a frozen RIB-In snapshot, then all advertisements are exchanged and
//     the next snapshot is built) repeated to a fixed point.
//
// The two must agree on every fixture's final best route for convergent
// scenarios; for oscillating scenarios both must report non-convergence.
// The comparator/policy spec is shared (it is the behavioral contract),
// but matching, selection and propagation are reimplemented here from
// scratch — no engine function is called.
package oracle

import (
	"fmt"
	"sort"
	"strings"

	"pvsim/config"
	"pvsim/model"
)

// Outcome is the oracle verdict.
type Outcome struct {
	Converged bool
	Rounds    int
	// Best: router -> prefix -> winning entry (empty winner when no route).
	Best map[string]map[string]Entry
	// CycleFrom/CycleTo are round numbers of a repeated state signature.
	CycleFrom, CycleTo int
	Signature          string
}

// Entry is one oracle RIB entry.
type Entry struct {
	Peer        string
	ASPath      []uint32
	LocalPref   int
	Med         int
	Origin      model.Origin
	SessionType string
	IGPCost     int
}

// Run evaluates a scenario synchronously. maxRounds bounds the search;
// when a state signature repeats, Converged=false with cycle indices.
func Run(sc *config.Scenario, maxRounds int) Outcome {
	if maxRounds <= 0 {
		maxRounds = 2000
	}
	// Collapse external events per (router,peer,prefix): last seq wins;
	// a final withdraw removes.
	type extKey struct {
		router, peer, prefix string
	}
	external := map[extKey]model.Attrs{}
	var seqOrder []*config.Event
	seqOrder = append(seqOrder, sc.Events...)
	sort.SliceStable(seqOrder, func(i, j int) bool { return seqOrder[i].Seq < seqOrder[j].Seq })
	for _, ev := range seqOrder {
		k := extKey{ev.Router, ev.Peer, ev.Prefix}
		if ev.Kind == "withdraw" {
			delete(external, k)
		} else {
			external[k] = ev.Attrs.Clone()
		}
	}

	// rib: router -> prefix -> peer -> entry. Seeded with external input.
	rib := map[string]map[string]map[string]Entry{}
	for k, attrs := range external {
		sess, igp, ok := sc.SessionBetween(k.router, k.peer)
		if !ok {
			continue // validated scenario; defensive
		}
		local, _ := sc.LookupRouter(k.router)
		if attrs.ContainsAS(local.ASN) {
			continue
		}
		attrs2, ok := importAccept(sc, k.router, sess, k.peer, model.Prefix(k.prefix), attrs)
		if !ok {
			continue
		}
		putRIB(rib, k.router, k.prefix, k.peer, Entry{
			Peer: k.peer, ASPath: append([]uint32(nil), attrs2.ASPath...),
			LocalPref: attrs2.LocalPrefOr(100), Med: attrs2.MedOr(),
			Origin: attrs2.Origin, SessionType: sess.Type, IGPCost: igp,
		})
	}

	seen := map[string]int{}
	var sig string
	best := map[string]map[string]Entry{}
	for round := 1; round <= maxRounds; round++ {
		// 1. Snapshot best choices from current RIB.
		best = bestChoice(sc, rib)
		sig = signature(sc, rib, best)
		if from, ok := seen[sig]; ok {
			return Outcome{Converged: false, Rounds: round, Best: best,
				CycleFrom: from, CycleTo: round, Signature: sig}
		}
		seen[sig] = round

		// 2. Exchange: build next RIB seeded from external input.
		next := map[string]map[string]map[string]Entry{}
		for k, attrs := range external {
			sess, igp, ok := sc.SessionBetween(k.router, k.peer)
			if !ok {
				continue
			}
			local, _ := sc.LookupRouter(k.router)
			if attrs.ContainsAS(local.ASN) {
				continue
			}
			attrs2, ok := importAccept(sc, k.router, sess, k.peer, model.Prefix(k.prefix), attrs)
			if !ok {
				continue
			}
			putRIB(next, k.router, k.prefix, k.peer, Entry{
				Peer: k.peer, ASPath: append([]uint32(nil), attrs2.ASPath...),
				LocalPref: attrs2.LocalPrefOr(100), Med: attrs2.MedOr(),
				Origin: attrs2.Origin, SessionType: sess.Type, IGPCost: igp,
			})
		}
		for _, r := range sc.Routers {
			prefixes := bestFor(best, r.Name)
			for _, p := range prefixes {
				win := best[r.Name][p]
				if win.Peer == "" {
					continue
				}
				outAttrs := model.Attrs{
					ASPath: append([]uint32(nil), win.ASPath...),
					Med:    intPtr(win.Med), Origin: win.Origin,
				}
				lp := win.LocalPref
				outAttrs.LocalPref = &lp
				for _, sess := range sc.Sessions {
					var peer string
					if sess.A == r.Name {
						peer = sess.B
					} else if sess.B == r.Name {
						peer = sess.A
					} else {
						continue
					}
					if peer == win.Peer && sess.Type == "ibgp" {
						continue // iBGP split-horizon only; eBGP relies on AS_PATH
					}
					pr := evalExport(sc, r.Name, sess, model.Prefix(p), outAttrs, peer)
					if !pr.ok {
						continue
					}
					sent := pr.attrs.Clone()
					if sess.Type == "ebgp" {
						sent.LocalPref = nil
						sent.ASPath = append([]uint32{r.ASN}, sent.ASPath...)
					}
					peerRouter, _ := sc.LookupRouter(peer)
					igp := sess.IGPCostA
					if sess.B == peer {
						igp = sess.IGPCostB
					}
					// inbound processing at peer (its loop check drops the
					// route if the path carries its own AS).
					inAttrs, ok := importAccept(sc, peer, sess, r.Name, model.Prefix(p), sent)
					if !ok {
						continue
					}
					if inAttrs.ContainsAS(peerRouter.ASN) {
						continue
					}
					putRIB(next, peer, p, r.Name, Entry{
						Peer: r.Name, ASPath: append([]uint32(nil), inAttrs.ASPath...),
						LocalPref: inAttrs.LocalPrefOr(100), Med: inAttrs.MedOr(),
						Origin: inAttrs.Origin, SessionType: sess.Type, IGPCost: igp,
					})
				}
			}
		}
		if sameRIB(rib, next) {
			best = bestChoice(sc, next)
			return Outcome{Converged: true, Rounds: round, Best: best}
		}
		rib = next
	}
	return Outcome{Converged: false, Rounds: maxRounds, Best: best, Signature: sig}
}

func bestFor(best map[string]map[string]Entry, router string) []string {
	out := make([]string, 0)
	if m := best[router]; m != nil {
		for p, e := range m {
			if e.Peer != "" {
				out = append(out, p)
			}
		}
		sort.Strings(out)
	}
	return out
}

// bestChoice picks the winning entry per router/prefix using the oracle's
// own implementation of the comparison order.
func bestChoice(sc *config.Scenario, rib map[string]map[string]map[string]Entry) map[string]map[string]Entry {
	out := map[string]map[string]Entry{}
	for _, r := range sc.Routers {
		m := map[string]Entry{}
		for prefix, entries := range rib[r.Name] {
			var winnerName string
			var winner Entry
			for _, peer := range orderedPeers(sc, entries) {
				e := entries[peer]
				if winnerName == "" || prefer(sc, e, winner) {
					winnerName, winner = peer, e
				}
			}
			if winnerName != "" {
				m[prefix] = winner
			} else {
				m[prefix] = Entry{}
			}
		}
		out[r.Name] = m
	}
	return out
}

func orderedPeers(sc *config.Scenario, m map[string]Entry) []string {
	names := make([]string, 0, len(m))
	for k := range m {
		names = append(names, k)
	}
	ord := map[string]int{}
	for _, r := range sc.Routers {
		ord[r.Name] = r.Ordinal
	}
	sort.Slice(names, func(i, j int) bool { return ord[names[i]] < ord[names[j]] })
	return names
}

// prefer reports whether a is better than b — an independent rendering of
// the documented comparison order.
func prefer(sc *config.Scenario, a, b Entry) bool {
	// 1 local pref
	if a.LocalPref != b.LocalPref {
		return a.LocalPref > b.LocalPref
	}
	// 2 as-path length
	if len(a.ASPath) != len(b.ASPath) {
		return len(a.ASPath) < len(b.ASPath)
	}
	// 3 med when same neighbor AS
	if len(a.ASPath) > 0 && len(b.ASPath) > 0 && a.ASPath[0] == b.ASPath[0] {
		if a.Med != b.Med {
			return a.Med < b.Med
		}
	}
	// 4 origin
	if a.Origin != b.Origin {
		return a.Origin < b.Origin
	}
	// 5 ebgp over ibgp
	if a.SessionType != b.SessionType {
		return a.SessionType == "ebgp"
	}
	// 6 igp cost
	if a.IGPCost != b.IGPCost {
		return a.IGPCost < b.IGPCost
	}
	// 7 router id (declaration ordinal)
	return ordinal(sc, a.Peer) < ordinal(sc, b.Peer)
}

func ordinal(sc *config.Scenario, name string) int {
	if r, ok := sc.LookupRouter(name); ok {
		return r.Ordinal
	}
	return 1 << 30
}

func putRIB(rib map[string]map[string]map[string]Entry, router, prefix, peer string, e Entry) {
	if rib[router] == nil {
		rib[router] = map[string]map[string]Entry{}
	}
	if rib[router][prefix] == nil {
		rib[router][prefix] = map[string]Entry{}
	}
	rib[router][prefix][peer] = e
}

// --- independent policy implementation -----------------------------------

func importAccept(sc *config.Scenario, at string, sess *config.Session,
	from string, p model.Prefix, attrs model.Attrs) (model.Attrs, bool) {
	var rules []*config.PolicyRule
	if sess.A == at {
		rules = sess.ImportA
	} else {
		rules = sess.ImportB
	}
	// local_pref is iBGP-only on the wire: drop any carried value BEFORE
	// the import policy runs, so a policy-set local_pref is preserved.
	in := attrs.Clone()
	if sess.Type == "ebgp" {
		in.LocalPref = nil
	}
	out, ok := applyRules(rules, p, in, from)
	if !ok {
		return out, false
	}
	if out.LocalPref == nil {
		lp := 100
		out.LocalPref = &lp
	}
	return out, true
}

type exportVerdict struct {
	ok    bool
	attrs model.Attrs
}

func evalExport(sc *config.Scenario, at string, sess *config.Session,
	p model.Prefix, attrs model.Attrs, target string) exportVerdict {
	var rules []*config.PolicyRule
	if sess.A == at {
		rules = sess.ExportA
	} else {
		rules = sess.ExportB
	}
	out, ok := applyRules(rules, p, attrs, target)
	return exportVerdict{ok: ok, attrs: out}
}

func applyRules(rules []*config.PolicyRule, p model.Prefix, attrs model.Attrs, peer string) (model.Attrs, bool) {
	cur := attrs.Clone()
	for _, r := range rules {
		if !ruleMatches(r.Match, p, cur, peer) {
			continue
		}
		if r.Deny {
			return cur, false
		}
		for _, a := range r.Actions {
			switch a.Type {
			case config.ActionSetLocalPref:
				v := *a.SetLocalPref
				cur.LocalPref = &v
			case config.ActionSetMed:
				v := *a.SetMed
				cur.Med = &v
			case config.ActionSetOrigin:
				o, _ := model.ParseOrigin(*a.SetOrigin)
				cur.Origin = o
			case config.ActionPrependAS:
				cur.ASPath = append(append([]uint32(nil), a.PrependAS...), cur.ASPath...)
			}
		}
		return cur, true
	}
	return cur, true
}

func ruleMatches(m config.Match, p model.Prefix, a model.Attrs, peer string) bool {
	if m.Prefix != "" && string(p) != m.Prefix {
		return false
	}
	if len(m.PrefixSet) > 0 {
		hit := false
		for _, x := range m.PrefixSet {
			if x == string(p) {
				hit = true
				break
			}
		}
		if !hit {
			return false
		}
	}
	if m.FromRouter != "" && m.FromRouter != peer {
		return false
	}
	if len(m.ASPathContains) > 0 {
		hit := false
		for _, x := range m.ASPathContains {
			if a.ContainsAS(x) {
				hit = true
				break
			}
		}
		if !hit {
			return false
		}
	}
	if m.ASPathEquals != nil {
		if len(m.ASPathEquals) != len(a.ASPath) {
			return false
		}
		for i := range m.ASPathEquals {
			if m.ASPathEquals[i] != a.ASPath[i] {
				return false
			}
		}
	}
	if m.ASPathLengthGT >= 0 && len(a.ASPath) <= m.ASPathLengthGT {
		return false
	}
	if m.ASPathLengthLT >= 0 && len(a.ASPath) >= m.ASPathLengthLT {
		return false
	}
	lp := a.LocalPrefOr(100)
	if m.LocalPrefGT != nil && lp <= *m.LocalPrefGT {
		return false
	}
	if m.LocalPrefLT != nil && lp >= *m.LocalPrefLT {
		return false
	}
	med := a.MedOr()
	if m.MedGT != nil && med <= *m.MedGT {
		return false
	}
	if m.MedLT != nil && med >= *m.MedLT {
		return false
	}
	if len(m.Origin) > 0 {
		hit := false
		for _, o := range m.Origin {
			if want, ok := model.ParseOrigin(o); ok && want == a.Origin {
				hit = true
			}
		}
		if !hit {
			return false
		}
	}
	return true
}

func intPtr(v int) *int { return &v }

// --- state identity --------------------------------------------------------

func sameRIB(a, b map[string]map[string]map[string]Entry) bool {
	return signatureRIB(a) == signatureRIB(b)
}

func signatureRIB(rib map[string]map[string]map[string]Entry) string {
	var sb strings.Builder
	routers := make([]string, 0, len(rib))
	for r := range rib {
		routers = append(routers, r)
	}
	sort.Strings(routers)
	for _, r := range routers {
		prefs := make([]string, 0)
		for p := range rib[r] {
			prefs = append(prefs, p)
		}
		sort.Strings(prefs)
		for _, p := range prefs {
			peers := make([]string, 0)
			for q := range rib[r][p] {
				peers = append(peers, q)
			}
			sort.Strings(peers)
			for _, q := range peers {
				e := rib[r][p][q]
				fmt.Fprintf(&sb, "%s|%s|%s|%v|%d|%d|%d;", r, p, q, e.ASPath, e.LocalPref, e.Med, e.Origin)
			}
		}
	}
	return sb.String()
}

func signature(sc *config.Scenario, rib map[string]map[string]map[string]Entry,
	best map[string]map[string]Entry) string {
	var sb strings.Builder
	for _, r := range sc.Routers {
		prefs := make([]string, 0)
		for p := range best[r.Name] {
			prefs = append(prefs, p)
		}
		sort.Strings(prefs)
		for _, p := range prefs {
			e := best[r.Name][p]
			fmt.Fprintf(&sb, "%s|%s|%s|%v;", r.Name, p, e.Peer, e.ASPath)
		}
	}
	sb.WriteString(signatureRIB(rib))
	return sb.String()
}
