package config

import (
	"pathvector/internal/model"
)

// Decision is the result of applying a policy chain.
type Decision struct {
	Permit bool
	// Rule is the name of the matched rule, or "default-permit".
	Rule string
}

// ruleResult records the first matched rule's effects.
type ruleResult struct {
	decision     Decision
	prependCount int
}

// applyRules runs one chain with first-match-wins semantics; no match means
// default permit. Rewrites mutate attrs only on a matched permit rule.
func applyRules(rules []Rule, prefix, peer string, attrs *model.Attrs) ruleResult {
	for _, r := range rules {
		if !r.Match.matches(prefix, peer) {
			continue
		}
		if !r.Action.Allow {
			return ruleResult{decision: Decision{Permit: false, Rule: r.Name}}
		}
		if r.Action.SetLocalPref != nil {
			attrs.LocalPref = *r.Action.SetLocalPref
		}
		if r.Action.SetMED != nil {
			attrs.MED = *r.Action.SetMED
		}
		return ruleResult{
			decision:     Decision{Permit: true, Rule: r.Name},
			prependCount: r.Action.Prepend,
		}
	}
	return ruleResult{decision: Decision{Permit: true, Rule: "default-permit"}}
}

// ApplyImport evaluates the import chain for a candidate just received from
// peer. On permit the (possibly rewritten) candidate is returned; on deny
// ok=false. Import rules cannot prepend (validated in config.Validate).
func ApplyImport(p *Policy, peer string, cand model.Candidate) (model.Candidate, Decision, bool) {
	if p == nil {
		return cand, Decision{Permit: true, Rule: "default-permit"}, true
	}
	res := applyRules(p.Import, cand.Prefix, peer, &cand.Attrs)
	return cand, res.decision, res.decision.Permit
}

// ExportAction records an export decision plus the number of extra self-AS
// prepends the engine must perform on top of the standard eBGP prepend.
type ExportAction struct {
	Decision     Decision
	PrependCount int
}

// ApplyExport evaluates the export chain for a candidate being sent to peer.
func ApplyExport(p *Policy, peer string, cand model.Candidate) (model.Candidate, ExportAction, bool) {
	if p == nil {
		return cand, ExportAction{Decision: Decision{Permit: true, Rule: "default-permit"}}, true
	}
	res := applyRules(p.Export, cand.Prefix, peer, &cand.Attrs)
	if !res.decision.Permit {
		return cand, ExportAction{Decision: res.decision}, false
	}
	return cand, ExportAction{Decision: res.decision, PrependCount: res.prependCount}, true
}
