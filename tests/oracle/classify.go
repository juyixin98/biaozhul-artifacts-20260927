package oracle

import (
	"strings"

	"fwrule/internal/config"
)

// famV4 is netmodel.FamV4 (4); kept local to avoid an import cycle of names.
const famV4 = 4

// RuleClass is the oracle-derived verdict for one rule, computed purely by
// brute-force enumeration: full shadow / partial shadow / redundant / clean.
type RuleClass struct {
	FullShadow    bool
	PartialShadow bool
	Redundant     bool
	// Removable: deleting the rule changes no packet action.
	Removable bool

	stolen        int
	essentialWins int // packets the rule wins and whose action would change
	redundantWins int // packets the rule wins but an equal-action successor decides
}

func (c *RuleClass) markStolen()       { c.stolen++ }
func (c *RuleClass) markEssentialWin() { c.essentialWins++ }
func (c *RuleClass) markRedundantWin() { c.redundantWins++ }

// Classify derives per-rule classes from exhaustive packet enumeration.
//
// For every packet p matching rule i record whether i is first match (wins p)
// or an earlier rule wins (stolen p). Then:
//   - full shadow:  i matches some packet but wins none;
//   - partial shadow: i wins some packet but is stolen for another;
//   - redundant:     i wins packets, and for EVERY packet it wins the
//     second-match (or the default) takes i's same action.
func Classify(pol *config.Policy, sp Space) map[int]*RuleClass {
	out := map[int]*RuleClass{}
	for _, r := range pol.Rules {
		out[r.Index] = &RuleClass{}
	}
	defaultAction, defaultOK := pol.DefaultFor(famV4)

	for _, p := range Enumerate(sp) {
		matches := Matching(pol, p)
		if len(matches) == 0 {
			continue
		}
		for pos, ri := range matches {
			c := out[ri]
			if pos == 0 {
				var alt config.Action
				if len(matches) >= 2 {
					alt = pol.Rules[matches[1]].Action
				} else if defaultOK {
					alt = defaultAction
				}
				if alt == pol.Rules[ri].Action {
					c.markRedundantWin()
				} else {
					c.markEssentialWin()
				}
			} else {
				c.markStolen()
			}
		}
	}
	for _, r := range pol.Rules {
		c := out[r.Index]
		wins := c.redundantWins + c.essentialWins
		if wins == 0 && c.stolen > 0 {
			c.FullShadow = true
		} else if wins > 0 && c.stolen > 0 {
			c.PartialShadow = true
		}
		c.Redundant = wins > 0 && c.essentialWins == 0
		c.Removable = c.FullShadow || c.Redundant
	}
	return out
}

// Explain renders a class as readable labels for test failure messages.
func (c *RuleClass) Explain() string {
	var labels []string
	if c.FullShadow {
		labels = append(labels, "FULL_SHADOW")
	}
	if c.PartialShadow {
		labels = append(labels, "PARTIAL_SHADOW")
	}
	if c.Redundant {
		labels = append(labels, "REDUNDANT")
	}
	if len(labels) == 0 {
		return "clean"
	}
	return strings.Join(labels, "+")
}
