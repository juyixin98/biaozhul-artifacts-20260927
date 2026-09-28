package oracle

// Kinds of defects (mirror of the service taxonomy, derived independently).
const (
	KindFull    = "fully_shadowed"
	KindPartial = "partially_shadowed"
	KindRedund  = "redundant"
)

// ExpectedDiag is a defect the reference model predicts for one rule in one
// family.
type ExpectedDiag struct {
	RuleID string
	Family string
	Kind   string
}

// EffectiveStats summarizes what a rule independently decides.
type EffectiveStats struct {
	Decided      int // packets for which this rule is the first matcher
	DecidedFlip  int // decided packets whose decision changes if removed
	TotalMatches int // packets in the rule's region (regardless of order)
}

// Stats computes per-rule, per-family statistics by exhaustive simulation.
func (m *Model) Stats(family string, packets []Pk) map[string]EffectiveStats {
	st := map[string]*EffectiveStats{}
	for _, r := range m.Rules {
		st[r.ID] = &EffectiveStats{}
	}
	removed := map[string]*Model{}
	for _, r := range m.Rules {
		removed[r.ID] = m.Without(r.ID)
	}
	for _, p := range packets {
		d := m.Decide(p)
		// total matches (order-insensitive)
		for _, r := range m.Rules {
			if Matches(r, p) {
				st[r.ID].TotalMatches++
			}
		}
		if d.ByDefault {
			continue
		}
		s := st[d.RuleID]
		s.Decided++
		if removed[d.RuleID].Decide(p).Action != d.Action {
			s.DecidedFlip++
		}
	}
	out := map[string]EffectiveStats{}
	for id, s := range st {
		out[id] = *s
	}
	return out
}

// ruleProjectsTo reports whether the rule exists in the given family.
func ruleProjectsTo(r Spec, family string) bool {
	for _, f := range ruleFamily(r) {
		if f == family {
			return true
		}
	}
	return false
}

// ExpectedDiagnostics returns the exact defect set the analyzer must report,
// for one family, over an exhaustive packet set.
//
// Independent definitions:
//
//   - fully_shadowed:     rule region is non-empty but the rule decides 0
//     packets (every matching packet is taken earlier).
//   - partially_shadowed: rule decides >0 packets but its region contains
//     packets it does not decide (some taken earlier).
//   - redundant:          rule decides >0 packets yet deleting it flips the
//     decision of none of them (later same-action rules
//     or the default action decide them identically).
//
// A rule can be redundant without being shadowed (e.g. an allow rule late in
// a default-allow chain); both kinds are then reported independently.
func ExpectedDiagnostics(m *Model, family string, packets []Pk) []ExpectedDiag {
	stats := m.Stats(family, packets)
	var out []ExpectedDiag
	for _, r := range m.Rules {
		if !ruleProjectsTo(r, family) {
			continue
		}
		s := stats[r.ID]
		switch {
		case s.Decided == 0 && s.TotalMatches > 0:
			out = append(out, ExpectedDiag{r.ID, family, KindFull})
		case s.Decided > 0 && s.TotalMatches > s.Decided:
			out = append(out, ExpectedDiag{r.ID, family, KindPartial})
		}
		if s.Decided > 0 && s.DecidedFlip == 0 {
			out = append(out, ExpectedDiag{r.ID, family, KindRedund})
		}
	}
	return out
}
