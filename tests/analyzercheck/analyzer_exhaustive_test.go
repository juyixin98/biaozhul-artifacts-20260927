package analyzercheck_test

import (
	"os"
	"testing"

	"fwrule/internal/analyzer"
	"fwrule/internal/config"

	// The oracle is an independent brute-force reference; importing it here is
	// exactly the point of the exercise.
	"fwrule/tests/oracle"
)

func loadMini(t *testing.T) *config.Policy {
	t.Helper()
	raw, err := os.ReadFile("../testdata/mini-policy.json")
	if err != nil {
		t.Fatal(err)
	}
	pol, err := config.Load(raw, "mini-policy.json")
	if err != nil {
		t.Fatal(err)
	}
	return pol
}

// miniSpace is the exhaustively enumerated universe:
//
//	src/dst IPs: the full /30 block of the mini rules (4 addresses) plus
//	             outside addresses that must hit the default;
//	ports 0..9:  covers every rule endpoint and the gaps around them;
//	protocols: tcp(6), udp(17) (port coordinates) and icmp(1)/gre(47).
func miniSpace() oracle.Space {
	ips := []uint32{
		0x0A000000, 0x0A000001, 0x0A000002, 0x0A000003, // 10.0.0.0/30
		0xC0A80000, 0xC0A80001, 0xC0A80002, 0xC0A80003, // 192.168.0.0/30
		0xAC100000, 0xAC100001, 0xAC100002, 0xAC100003, // 172.16.0.0/30
		0xC0A80100, 0xC0A80101, 0xC0A80102, 0xC0A80103, // 192.168.1.0/30
	}
	outside := []uint32{0x08080808, 0xC0A80209} // never inside any block
	var ports []uint16
	for p := uint16(0); p <= 9; p++ {
		ports = append(ports, p)
	}
	return oracle.Space{
		SrcIPs:     ips,
		DstIPs:     ips,
		Ports:      ports,
		Protos:     []uint8{6, 17, 1, 47},
		OutsideIPs: outside,
	}
}

func kindsByRule(rep *analyzer.Report) map[string]map[analyzer.Category]bool {
	m := map[string]map[analyzer.Category]bool{}
	for _, d := range rep.Diagnostics {
		if m[d.RuleID] == nil {
			m[d.RuleID] = map[analyzer.Category]bool{}
		}
		m[d.RuleID][d.Kind] = true
	}
	return m
}

// TestExpectedCategories asserts the EXACT expected failure class for each
// rule of the mini policy — this is a concrete-result assertion, not an
// interface smoke test.
func TestExpectedCategories(t *testing.T) {
	pol := loadMini(t)
	rep := analyzer.Analyze(pol, 0)
	k := kindsByRule(rep)

	expect := map[string][]analyzer.Category{
		"a-broad-allow":    nil,
		"b-full-shadow":    {analyzer.CatFullShadow},
		"c-partial-shadow": {analyzer.CatPartialShadow},
		// d wins exactly dst port 4/src ports 0-1; e wins dst port 5. On the
		// overlap (4, src ports 2-3) e is later, so d also steals 2-3 from e:
		// e is partially shadowed by d despite being the broader rule.
		"d-narrow-allow":  {analyzer.CatRedundant},
		"e-broader-allow": {analyzer.CatPartialShadow},
		"f-cross-allow":   nil,
		// g wins exactly the overlapping dst port 1; its other winning region
		// (dst port 0) falls through to the default deny, same action, so g
		// is both partially shadowed AND redundant.
		"g-cross-deny": {analyzer.CatPartialShadow, analyzer.CatRedundant},
		"h-udp-allow":  nil,
		"i-icmp-allow": nil,
		// j never wins on ports 0-4 (earlier rules do); it only wins 6-9,
		// which the default would deny anyway.
		"j-tail-deny-default-echo": {analyzer.CatPartialShadow, analyzer.CatRedundant},
	}
	for id, wants := range expect {
		got := k[id]
		for _, w := range wants {
			if !got[w] {
				t.Errorf("rule %s: expected diagnostic %s, got set=%v", id, w, got)
			}
		}
		if len(got) != len(wants) {
			t.Errorf("rule %s: unexpected diagnostics: got=%v want=%v", id, got, wants)
		}
	}
}

// TestRemovableSetExhaustive is the central correctness claim: deleting
// EXACTLY the rules the analyzer marks removable changes no decision for any
// packet, verified by the independent oracle over the full mini space.
func TestRemovableSetExhaustive(t *testing.T) {
	pol := loadMini(t)
	rep := analyzer.Analyze(pol, 0)
	sp := miniSpace()

	original, _ := oracle.EvalAll(pol, sp)

	removable := map[int]bool{}
	for _, r := range rep.Rules {
		if r.Removable {
			removable[r.Index] = true
		}
	}
	wantRemoved := map[string]bool{
		"b-full-shadow":            true,
		"d-narrow-allow":           true,
		"g-cross-deny":             true,
		"j-tail-deny-default-echo": true,
	}
	gotRemoved := map[string]bool{}
	for idx := range removable {
		gotRemoved[pol.Rules[idx].ID] = true
	}
	if len(gotRemoved) != len(wantRemoved) {
		t.Fatalf("removable set = %v, want %v", gotRemoved, wantRemoved)
	}
	for id := range wantRemoved {
		if !gotRemoved[id] {
			t.Fatalf("expected %s to be removable", id)
		}
	}

	pruned := oracle.WithoutPolicy(pol, removable)
	prunedMap, _ := oracle.EvalAll(pruned, sp)
	if ok, why, pkt := oracle.Equivalent(original, prunedMap); !ok {
		t.Fatalf("pruning changed behavior: %s at %s", why, pkt)
	}

	// And the inverse: removing any rule the analyzer did NOT mark removable
	// must change at least one packet's decision.
	for _, r := range rep.Rules {
		if removable[r.Index] {
			continue
		}
		oneOff := oracle.WithoutPolicy(pol, map[int]bool{r.Index: true})
		oneOffMap, _ := oracle.EvalAll(oneOff, sp)
		if ok, _, _ := oracle.Equivalent(original, oneOffMap); ok {
			t.Fatalf("analyzer kept %s but deleting it changes nothing", r.RuleID)
		}
	}
}

// TestWitnessesAreReal replays every emitted witness through the independent
// oracle and checks the claimed decision and winning rule.
func TestWitnessesAreReal(t *testing.T) {
	pol := loadMini(t)
	rep := analyzer.Analyze(pol, 0)
	idByIndex := map[int]string{}
	for _, r := range pol.Rules {
		idByIndex[r.Index] = r.ID
	}
	for _, d := range rep.Diagnostics {
		w := d.Witness
		if w == nil {
			if d.Kind != analyzer.CatEmptyMatch {
				t.Errorf("%s: missing witness", d.RuleID)
			}
			continue
		}
		src, _, err := parseIPv4(w.SrcIP)
		if err != nil {
			t.Fatalf("%s witness src: %v", d.RuleID, err)
		}
		dst, _, err := parseIPv4(w.DstIP)
		if err != nil {
			t.Fatalf("%s witness dst: %v", d.RuleID, err)
		}
		pkt := oracle.Packet{
			ProtoNum: w.Protocol, SrcIP: src, DstIP: dst,
			SrcPort: w.SrcPort, DstPort: w.DstPort,
		}
		out := oracle.Decide(pol, pkt)
		gotWinner := "default"
		if out.Winner >= 0 {
			gotWinner = idByIndex[out.Winner]
		}
		if out.Winner >= 0 && gotWinner != w.DecidedBy && d.Kind != analyzer.CatRedundant {
			t.Errorf("%s witness %s decided by %s, witness claims %s",
				d.RuleID, pkt, gotWinner, w.DecidedBy)
		}
		if string(out.Action) != w.Action {
			t.Errorf("%s witness %s action got=%s claim=%s",
				d.RuleID, pkt, out.Action, w.Action)
		}
	}
}
