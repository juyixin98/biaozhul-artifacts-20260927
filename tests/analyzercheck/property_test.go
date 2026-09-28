package analyzercheck_test

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"testing"

	"fwrule/internal/analyzer"
	"fwrule/internal/config"
	"fwrule/tests/oracle"
)

// Universe aligned with the generated rules (see genPolicy):
//
//	src blocks: 10.0.0.0/30, 172.16.0.0/30, 0.0.0.0/0
//	dst blocks: 192.168.0.0/30, 192.168.1.0/30
//	dst/src ports: rules only use endpoints inside 0..9; the universe spans
//	               0..12 to include packets outside every port interval;
//	protocols: tcp(6), udp(17), icmp(1), gre(47). Rules name only the first
//	           three concretely; gre is the analyzer's any-rule witness
//	           protocol and is enumerated by the oracle as well.
func propertySpace() oracle.Space {
	ips := []uint32{
		0x0A000000, 0x0A000001, 0x0A000002, 0x0A000003,
		0xAC100000, 0xAC100001, 0xAC100002, 0xAC100003,
		0xC0A80000, 0xC0A80001, 0xC0A80002, 0xC0A80003,
		0xC0A80100, 0xC0A80101, 0xC0A80102, 0xC0A80103,
	}
	outside := []uint32{0x08080808}
	var ports []uint16
	for p := uint16(0); p <= 10; p++ {
		ports = append(ports, p)
	}
	return oracle.Space{
		SrcIPs: ips, DstIPs: ips, OutsideIPs: outside,
		Ports: ports, Protos: []uint8{6, 17, 1, 47},
	}
}

var (
	srcChoices   = []string{"10.0.0.0/30", "172.16.0.0/30", "0.0.0.0/0"}
	dstChoices   = []string{"192.168.0.0/30", "192.168.1.0/30", "0.0.0.0/0"}
	portChoices  = []string{"0-3", "2-5", "6-9", "0-9", "4-7"}
	protoChoices = []string{"tcp", "udp", "icmp", "any"}
)

func genPolicy(rng *rand.Rand, n int) config.RuleSpec {
	spec := config.RuleSpec{
		ID:       fmt.Sprintf("gen-%02d", n),
		Action:   []string{"allow", "deny"}[rng.Intn(2)],
		Protocol: protoChoices[rng.Intn(len(protoChoices))],
		SrcCIDR:  srcChoices[rng.Intn(len(srcChoices))],
		DstCIDR:  dstChoices[rng.Intn(len(dstChoices))],
	}
	if spec.Protocol == "tcp" || spec.Protocol == "udp" {
		spec.SrcPort = portChoices[rng.Intn(len(portChoices))]
		spec.DstPort = portChoices[rng.Intn(len(portChoices))]
	}
	return spec
}

func TestRandomPoliciesMatchOracle(t *testing.T) {
	const iterations = 50
	sp := propertySpace()
	for seed := int64(1); seed <= iterations; seed++ {
		rng := rand.New(rand.NewSource(seed))
		nRules := 5 + rng.Intn(5) // 5..9 rules
		rules := make([]config.RuleSpec, nRules)
		for i := 0; i < nRules; i++ {
			rules[i] = genPolicy(rng, i)
		}
		defAction := []string{"deny", "allow"}[rng.Intn(2)]
		doc := map[string]any{
			"name":           fmt.Sprintf("random-%d", seed),
			"default_action": map[string]string{"ipv4": defAction},
			"rules":          rules,
		}
		raw, _ := json.Marshal(doc)
		pol, err := config.Load(raw, "generated")
		if err != nil {
			t.Fatalf("seed %d: generated policy failed to compile: %v", seed, err)
		}

		// Independent brute-force classification.
		oc := oracle.Classify(pol, sp)

		// Analyzer classification.
		rep := analyzer.Analyze(pol, seed)
		kinds := map[string]map[analyzer.Category]bool{}
		removable := map[string]bool{}
		for _, r := range rep.Rules {
			removable[r.RuleID] = r.Removable
		}
		for _, d := range rep.Diagnostics {
			if kinds[d.RuleID] == nil {
				kinds[d.RuleID] = map[analyzer.Category]bool{}
			}
			kinds[d.RuleID][d.Kind] = true
		}

		for _, r := range pol.Rules {
			got := kinds[r.ID]
			want := oc[r.Index]
			checks := []struct {
				name string
				got  bool
				want bool
			}{
				{"FULL_SHADOW", got[analyzer.CatFullShadow], want.FullShadow},
				{"PARTIAL_SHADOW", got[analyzer.CatPartialShadow], want.PartialShadow},
				{"REDUNDANT", got[analyzer.CatRedundant], want.Redundant},
				{"removable", removable[r.ID], want.Removable},
			}
			for _, c := range checks {
				if c.got != c.want {
					t.Fatalf("seed=%d rule=%s %s: analyzer=%v oracle=%v\npolicy=%s\noracle=%s",
						seed, r.ID, c.name, c.got, c.want, string(raw), want.Explain())
				}
			}
		}

		// Behavioral cross-check. Redundancy is a SINGLE-RULE property: r is
		// removable when deleting r alone leaves every decision unchanged.
		// It is NOT closed under union (two rules where each is the other's
		// same-action successor cannot both be removed at once), so each
		// removable rule is checked by its individual deletion.
		original, _ := oracle.EvalAll(pol, sp)
		for _, r := range pol.Rules {
			oneOff, _ := oracle.EvalAll(
				oracle.WithoutPolicy(pol, map[int]bool{r.Index: true}), sp)
			ok, why, pkt := oracle.Equivalent(original, oneOff)
			if removable[r.ID] && !ok {
				t.Fatalf("seed=%d rule %s marked removable but deleting it changed behavior: %s at %s\npolicy=%s",
					seed, r.ID, why, pkt, string(raw))
			}
			if !removable[r.ID] && ok {
				t.Fatalf("seed=%d rule %s kept but deleting it changes nothing\npolicy=%s",
					seed, r.ID, string(raw))
			}
		}
	}
}
