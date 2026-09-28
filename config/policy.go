package config

import (
	"pvsim/model"
)

// PolicyInput is the route context handed to a policy evaluator.
type PolicyInput struct {
	Prefix     model.Prefix
	Attrs      model.Attrs
	FromRouter string // direct peer for import; the target peer for export matches
}

// PolicyResult is the outcome of evaluating an ordered rule list.
type PolicyResult struct {
	Permitted bool
	Attrs     model.Attrs // mutated copy when permitted
	RuleName  string      // deciding rule; "" means default permit
	Deny      bool        // true when a deny rule matched
}

// EvalPolicy evaluates a first-match policy chain against a route.
//
// Semantics (documented in README):
//   - rules are evaluated in declaration order;
//   - the first matching rule decides: deny -> drop, permit -> apply its
//     actions in order and accept;
//   - no rule matches -> permit unmodified.
func EvalPolicy(rules []*PolicyRule, in PolicyInput) PolicyResult {
	attrs := in.Attrs.Clone()
	for _, r := range rules {
		if !matchRule(r.Match, in) {
			continue
		}
		if r.Deny {
			return PolicyResult{Permitted: false, Attrs: attrs, RuleName: r.Name, Deny: true}
		}
		for _, a := range r.Actions {
			applyAction(&attrs, a)
		}
		return PolicyResult{Permitted: true, Attrs: attrs, RuleName: r.Name}
	}
	return PolicyResult{Permitted: true, Attrs: attrs}
}

func matchRule(m Match, in PolicyInput) bool {
	if m.Prefix != "" && string(in.Prefix) != m.Prefix {
		return false
	}
	if len(m.PrefixSet) > 0 {
		found := false
		for _, p := range m.PrefixSet {
			if p == string(in.Prefix) {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	if m.FromRouter != "" && m.FromRouter != in.FromRouter {
		return false
	}
	a := in.Attrs
	if len(m.ASPathContains) > 0 {
		found := false
		for _, want := range m.ASPathContains {
			if a.ContainsAS(want) {
				found = true
				break
			}
		}
		if !found {
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
	if m.ASPathLengthGT >= 0 && !(len(a.ASPath) > m.ASPathLengthGT) {
		return false
	}
	if m.ASPathLengthLT >= 0 && !(len(a.ASPath) < m.ASPathLengthLT) {
		return false
	}
	if m.LocalPrefGT != nil && !(a.LocalPrefOr(100) > *m.LocalPrefGT) {
		return false
	}
	if m.LocalPrefLT != nil && !(a.LocalPrefOr(100) < *m.LocalPrefLT) {
		return false
	}
	if m.MedGT != nil && !(a.MedOr() > *m.MedGT) {
		return false
	}
	if m.MedLT != nil && !(a.MedOr() < *m.MedLT) {
		return false
	}
	if len(m.Origin) > 0 {
		found := false
		for _, o := range m.Origin {
			if want, ok := model.ParseOrigin(o); ok && want == a.Origin {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}

func applyAction(attrs *model.Attrs, a Action) {
	switch a.Type {
	case ActionSetLocalPref:
		v := *a.SetLocalPref
		attrs.LocalPref = &v
	case ActionSetMed:
		v := *a.SetMed
		attrs.Med = &v
	case ActionSetOrigin:
		o, _ := model.ParseOrigin(*a.SetOrigin)
		attrs.Origin = o
	case ActionPrependAS:
		// Prepend in listed order, left to right: [x,y] prepended to path
		// [z] yields [x,y,z].
		attrs.ASPath = append(append([]uint32(nil), a.PrependAS...), attrs.ASPath...)
	}
}
