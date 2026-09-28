package config

import (
	"fmt"
	"sort"
	"strings"

	"pvsim/model"
)

// validate checks cross-reference and semantic consistency of the parsed
// scenario. It is the single place that produces UNREFERENCED_ENTITY errors.
func (s *Scenario) validate() error {
	if len(s.Routers) == 0 {
		return model.NewError(model.KindInput, "INVALID_CONFIG", "scenario declares no routers")
	}
	if len(s.Events) == 0 {
		return model.NewError(model.KindInput, "INVALID_CONFIG", "scenario contains no events")
	}
	if s.MaxSteps < 0 {
		return model.NewError(model.KindInput, "INVALID_CONFIG", "max_steps must be >= 0 (0 = default)")
	}
	names := map[string]bool{}
	for _, r := range s.Routers {
		if r.Name == "" {
			return model.NewError(model.KindInput, "INVALID_CONFIG", "router #%d has empty name", r.Ordinal+1)
		}
		if names[r.Name] {
			return model.NewError(model.KindInput, "INVALID_CONFIG", "duplicate router name %q", r.Name)
		}
		names[r.Name] = true
		if r.ASN == 0 {
			return model.NewError(model.KindInput, "INVALID_CONFIG", "router %q: asn must be 1..4294967295", r.Name)
		}
	}
	ids := map[string]bool{}
	links := map[string]bool{}
	for _, sess := range s.Sessions {
		if ids[sess.ID] {
			return model.NewError(model.KindInput, "INVALID_CONFIG", "duplicate session id %q", sess.ID)
		}
		ids[sess.ID] = true
		if !names[sess.A] {
			return model.NewError(model.KindInput, "UNREFERENCED_ENTITY",
				"session %q: unknown router %q", sess.ID, sess.A)
		}
		if !names[sess.B] {
			return model.NewError(model.KindInput, "UNREFERENCED_ENTITY",
				"session %q: unknown router %q", sess.ID, sess.B)
		}
		if sess.A == sess.B {
			return model.NewError(model.KindInput, "INVALID_CONFIG",
				"session %q: a and b must differ", sess.ID)
		}
		key := linkKey(sess.A, sess.B)
		if links[key] {
			return model.NewError(model.KindInput, "INVALID_CONFIG",
				"routers %s share multiple sessions; the engine models a single BGP adjacency per pair", key)
		}
		links[key] = true
		ra := s.routerByName[sess.A]
		rb := s.routerByName[sess.B]
		if sess.Type == "ibgp" && ra.ASN != rb.ASN {
			return model.NewError(model.KindInput, "INVALID_CONFIG",
				"session %q: ibgp requires same ASN (%s AS%d vs %s AS%d)",
				sess.ID, sess.A, ra.ASN, sess.B, rb.ASN)
		}
		if sess.Type == "ebgp" && ra.ASN == rb.ASN {
			return model.NewError(model.KindInput, "INVALID_CONFIG",
				"session %q: ebgp requires different ASN (both AS%d)", sess.ID, ra.ASN)
		}
		for _, m := range allMatches(sess) {
			if m.FromRouter != "" && !names[m.FromRouter] {
				return model.NewError(model.KindInput, "UNREFERENCED_ENTITY",
					"session %q: match.from_router references unknown router %q", sess.ID, m.FromRouter)
			}
		}
	}
	seqs := map[int]bool{}
	for i, e := range s.Events {
		ra, ok := s.routerByName[e.Router]
		if !ok {
			return model.NewError(model.KindInput, "UNREFERENCED_ENTITY",
				"event #%d: unknown router %q", i+1, e.Router)
		}
		if _, ok := s.routerByName[e.Peer]; !ok {
			return model.NewError(model.KindInput, "UNREFERENCED_ENTITY",
				"event seq=%d: unknown peer %q", e.Seq, e.Peer)
		}
		sess, _, ok := s.SessionBetween(e.Router, e.Peer)
		if !ok {
			return model.NewError(model.KindInput, "UNREFERENCED_ENTITY",
				"event seq=%d: no session between %s and %s", e.Seq, e.Router, e.Peer)
		}
		if e.Kind == "update" && !e.AttrsSet {
			return model.NewError(model.KindInput, "INVALID_CONFIG",
				"event seq=%d: update requires attrs", e.Seq)
		}
		// local_pref is iBGP-only; an eBGP peer must not hand it to us.
		if e.Kind == "update" && sess.Type == "ebgp" && e.Attrs.LocalPref != nil {
			return model.NewError(model.KindInput, "INVALID_CONFIG",
				"event seq=%d: local_pref on eBGP event is not allowed (iBGP-only attribute)", e.Seq)
		}
		// Inbound AS_PATH loop on the event itself is rejected here as a
		// scenario error: a real speaker would silently drop it, making the
		// fixture confusing; fixtures should exercise loop rejection
		// through propagation (see fixture B / loop unit tests).
		if e.Kind == "update" && e.Attrs.ContainsAS(ra.ASN) {
			return model.NewError(model.KindInput, "LOOP_IN_EVENT",
				"event seq=%d: AS_PATH %v already contains local AS%d of %s",
				e.Seq, e.Attrs.ASPath, ra.ASN, e.Router)
		}
		if seqs[e.Seq] {
			return model.NewError(model.KindInput, "INVALID_CONFIG",
				"duplicate event seq %d", e.Seq)
		}
		seqs[e.Seq] = true
	}
	return nil
}

func linkKey(a, b string) string {
	x := []string{a, b}
	sort.Strings(x)
	return strings.Join(x, "--")
}

func allMatches(sess *Session) []Match {
	var out []Match
	collect := func(rules []*PolicyRule) {
		for _, r := range rules {
			out = append(out, r.Match)
		}
	}
	collect(sess.ImportA)
	collect(sess.ImportB)
	collect(sess.ExportA)
	collect(sess.ExportB)
	return out
}

// describeEvents is a small debug helper used by engine error messages.
func (s *Scenario) DescribeEvent(e *Event) string {
	return fmt.Sprintf("seq=%d %s<-%s %s %s", e.Seq, e.Router, e.Peer, e.Kind, e.Prefix)
}
