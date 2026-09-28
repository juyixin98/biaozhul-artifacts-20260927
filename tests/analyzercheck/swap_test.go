package analyzercheck_test

import (
	"os"
	"testing"

	"fwrule/internal/analyzer"
	"fwrule/internal/config"
	"fwrule/tests/oracle"
)

// TestRuleSwapDisjoint: swapping rules whose match sets are disjoint (here:
// different protocol families of rules, tcp vs udp vs icmp) must leave every
// packet decision identical — order only matters for rules that can both
// match the same packet.
func TestRuleSwapDisjoint(t *testing.T) {
	pol := loadMini(t)
	sp := miniSpace()
	before, _ := oracle.EvalAll(pol, sp)

	// Move udp rule h and icmp rule i ahead of the tcp rules. These rules
	// share no packet with tcp rules (protocol dimension is disjoint).
	swapped := &config.Policy{Name: pol.Name, Default: pol.Default}
	var order []*config.CompiledRule
	for _, r := range pol.Rules {
		if r.ID == "h-udp-allow" || r.ID == "i-icmp-allow" {
			order = append(order, r)
		}
	}
	for _, r := range pol.Rules {
		if r.ID != "h-udp-allow" && r.ID != "i-icmp-allow" {
			order = append(order, r)
		}
	}
	swapped.Rules = order
	after, _ := oracle.EvalAll(swapped, sp)
	if ok, why, pkt := oracle.Equivalent(before, after); !ok {
		t.Fatalf("disjoint-protocol swap changed behavior: %s at %s", why, pkt)
	}
}

// TestRuleSwapOverlappingChanges: swapping two OVERLAPPING, conflicting rules
// changes the decisions — this is the control case proving the test harness
// can detect order sensitivity (so the disjoint test above is meaningful).
func TestRuleSwapOverlappingChanges(t *testing.T) {
	pol := loadMini(t)
	sp := miniSpace()
	before, _ := oracle.EvalAll(pol, sp)

	// a-broad-allow (allow) and c-partial-shadow (deny) overlap on the
	// 2-3/2-3 region. Swapping c ahead of a must flip that region to deny.
	swapped := &config.Policy{Name: pol.Name, Default: pol.Default}
	var a, c *config.CompiledRule
	for _, r := range pol.Rules {
		switch r.ID {
		case "a-broad-allow":
			a = r
		case "c-partial-shadow":
			c = r
		}
	}
	cc := *c
	cc.Index = a.Index
	aa := *a
	aa.Index = c.Index
	for _, r := range pol.Rules {
		switch r.ID {
		case "a-broad-allow":
			swapped.Rules = append(swapped.Rules, &cc)
		case "c-partial-shadow":
			swapped.Rules = append(swapped.Rules, &aa)
		default:
			swapped.Rules = append(swapped.Rules, r)
		}
	}
	after, _ := oracle.EvalAll(swapped, sp)
	if ok, _, _ := oracle.Equivalent(before, after); ok {
		t.Fatal("overlapping allow/deny swap unexpectedly preserved behavior")
	}
}

// TestSwappedPolicyFile: the generated adjacent-swap fixture must produce a
// valid analysis report and, where the swap moved an overlapping deny ahead
// of an allow, the analyzer must report new shadowing — analysis adapts to
// order rather than keying on rule id/text.
func TestSwappedPolicyFile(t *testing.T) {
	raw, err := os.ReadFile("../testdata/mini-policy-swapped.json")
	if err != nil {
		t.Fatal(err)
	}
	pol, err := config.Load(raw, "swapped")
	if err != nil {
		t.Fatal(err)
	}
	rep := analyzer.Analyze(pol, 0)
	k := kindsByRule(rep)
	// In the swapped file b-full-shadow now precedes a-broad-allow and fully
	// covers the 0-1/0-1 region, so a must become partially shadowed there.
	if !k["a-broad-allow"][analyzer.CatPartialShadow] {
		t.Errorf("a-broad-allow should be partially shadowed after swap, got %v",
			k["a-broad-allow"])
	}
}
