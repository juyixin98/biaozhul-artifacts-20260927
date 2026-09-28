package analyzer

import (
	"sort"

	"fwrule/internal/config"
	"fwrule/internal/netmodel"
)

// ruleState accumulates everything the partition enumeration learned about
// one rule: every homogeneous cell it matches, split into cells it wins
// (first-match) and cells stolen by an earlier rule.
type ruleState struct {
	idx     int
	matched []cellRec
	wins    []cellRec
}

func (s *ruleState) init(i int) { s.idx = i }

// reachable: the rule is first-match for at least one concrete packet.
func (s *ruleState) reachable() bool { return len(s.wins) > 0 }

// fullyShadowed: the rule has a non-empty match set (cells exist) but wins
// none of them.
func (s *ruleState) fullyShadowed() bool {
	return len(s.matched) > 0 && len(s.wins) == 0
}

// removable proves deletion equivalence for rule s over its winning cells.
// For every cell it wins, the runner-up (second matching rule, or the family
// default when no other rule matches) must take the SAME action.
func (s *ruleState) removable(pol *config.Policy) (bool, string) {
	for _, rec := range s.wins {
		var alt config.Action
		if rec.runner >= 0 {
			alt = pol.Rules[rec.runner].Action
		} else {
			alt = rec.defaultAction
			if alt == "" {
				return false, "family default not configured for one covered region"
			}
		}
		if alt != pol.Rules[s.idx].Action {
			return false, ""
		}
	}
	return true, ""
}

// sortCells orders cells deterministically (family, protocol, src, dst,
// dst-port, src-port).
func sortCells(cs []cellRec) {
	sort.SliceStable(cs, func(i, j int) bool {
		a, b := cs[i].cell, cs[j].cell
		if a.Fam != b.Fam {
			return a.Fam < b.Fam
		}
		if a.ProtoNum != b.ProtoNum {
			return a.ProtoNum < b.ProtoNum
		}
		if a.SrcNet.First != b.SrcNet.First {
			return netmodelLess(a.SrcNet.First, b.SrcNet.First)
		}
		if a.DstNet.First != b.DstNet.First {
			return netmodelLess(a.DstNet.First, b.DstNet.First)
		}
		if a.DstPorts.Lo != b.DstPorts.Lo {
			return a.DstPorts.Lo < b.DstPorts.Lo
		}
		return a.SrcPorts.Lo < b.SrcPorts.Lo
	})
}

func netmodelLess(a, b netmodel.Addr) bool {
	if a.H != b.H {
		return a.H < b.H
	}
	return a.L < b.L
}
