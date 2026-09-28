package indcheck_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"netsem-test/harness"
	"netsem-test/oracle"
)

// Tiny, fully enumerable fixtures.
const (
	netA = "10.0.0.0/30" // .0 .. .3
	netB = "10.0.0.4/30" // .4 .. .7
	netC = "10.0.0.8/30" // .8 .. .11
	netD = "10.0.1.0/30" // disjoint /24

	netA6 = "2001:db8::/126"
	netB6 = "2001:db9::/126"
)

func fullPorts() oracle.PortRange { return oracle.PortRange{Lo: 0, Hi: 65535} }

// Scenario 1: crossing port intervals, full + partial shadowing, and a rule
// redundant with the default-deny. IPv4 only; the packet space below is
// exhaustively enumerated.
func TestScenario1_FullPartialRedundant(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	m := &oracle.Model{
		DefaultAction: "deny",
		Rules: []oracle.Spec{
			{ID: "r1", Action: "allow", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 81}},
			{ID: "r2", Action: "allow", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 81, Hi: 82}},
			{ID: "r3", Action: "allow", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 82, Hi: 83}},
			// r4 identical to r1: fully shadowed.
			{ID: "r4", Action: "allow", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 81}},
			// r5 denies what default deny already denies (unmatched region):
			// redundant with the default action.
			{ID: "r5", Action: "deny", Proto: "udp", SrcCIDRs: []string{netC}, DstCIDRs: []string{netD},
				SrcPorts: fullPorts(), DstPorts: fullPorts()},
		},
	}
	cr := submit(t, h, m)
	if !cr.ParseOK {
		t.Fatalf("parse errors: %+v", cr.ParseErrors)
	}

	srcs := oracle.Addrs("ipv4", netA, netC)
	dsts := oracle.Addrs("ipv4", netB, netD)
	u := oracle.Universe{
		Family: "ipv4", Protos: []int{6, 17},
		SrcAddrs: srcs, DstAddrs: dsts,
		SrcPorts: []int{0, 80, 81, 82, 83, 443},
		DstPorts: []int{79, 80, 81, 82, 83, 84, 443},
	}
	pks := oracle.Enumerate(u)

	assertExhaustive(t, h, m, pks)
	assertDiagnostics(t, cr, m, map[string][]oracle.Pk{"ipv4": pks})

	// Explicit, concrete assertions on categories (not just set comparison).
	var kinds []string
	for _, d := range cr.Report.Diagnostics {
		if d.Family == "ipv4" {
			kinds = append(kinds, d.RuleID+":"+d.Kind)
		}
	}
	assertContains(t, kinds, "r2:partially_shadowed")
	assertContains(t, kinds, "r2:redundant") // its live port 82 is allowed by later r3
	assertContains(t, kinds, "r3:partially_shadowed")
	assertContains(t, kinds, "r4:fully_shadowed")
	assertContains(t, kinds, "r5:redundant")
	assertNotContains(t, kinds, "r1:partially_shadowed")
	assertNotContains(t, kinds, "r1:fully_shadowed")
	// r1 is individually redundant ONLY because duplicate r4 still carries
	// its allow decision after r1 is deleted; remove the shadowed duplicate
	// first and r1 becomes load-bearing. This ordering dependency is part of
	// the semantics and is exercised by the deletion-invariance loop.
	assertContains(t, kinds, "r1:redundant")
	assertNotContains(t, kinds, "r3:redundant")
	assertNotContains(t, kinds, "r4:redundant") // decides nothing; classified as shadowed

	// Partial witness for r3 must be a packet really decided earlier.
	for _, d := range cr.Report.Diagnostics {
		if d.RuleID == "r3" && d.Kind == "partially_shadowed" {
			if d.Witness.DestinationPort != 82 || d.Witness.Protocol != "tcp" {
				t.Fatalf("r3 witness not the shared crossing packet: %+v", d.Witness)
			}
			if d.Witness.MatchedRuleID != "r2" {
				t.Fatalf("r3 witness should be matched by r2, got %q", d.Witness.MatchedRuleID)
			}
			if len(d.Covered) == 0 || len(d.Covered[0].Products) == 0 {
				t.Fatalf("r3 covered partition missing")
			}
			// Covered partition must show destination port 83 (its live
			// remainder), not 82.
			found83 := false
			for _, pp := range d.Covered[0].Products {
				if strings.Contains(pp.DestinationPorts, "83") {
					found83 = true
				}
				if strings.Contains(pp.DestinationPorts, "82") && !strings.Contains(pp.DestinationPorts, "83") {
					t.Fatalf("covered partition still lists shadowed port 82: %s", pp.DestinationPorts)
				}
			}
			if !found83 {
				t.Fatalf("covered partition does not show live port 83")
			}
		}
		if d.RuleID == "r4" && d.Kind == "fully_shadowed" {
			if d.Witness.MatchedRuleID != "r1" {
				t.Fatalf("r4 witness should cite r1, got %q", d.Witness.MatchedRuleID)
			}
		}
	}

	assertDeletionInvariance(t, h, m, map[string][]oracle.Pk{"ipv4": pks}, cr)
}

// Scenario 2: swapping two rules changes which one is shadowed; the behavior
// of the *packet function* is identical, but diagnostics follow first-match.
func TestScenario2_RuleSwap(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	base := []oracle.Spec{
		{ID: "wide", Action: "allow", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
			SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 90}},
		{ID: "narrow", Action: "deny", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
			SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 85, Hi: 85}},
	}

	mk := func(order []string) *oracle.Model {
		byID := map[string]oracle.Spec{}
		for _, s := range base {
			byID[s.ID] = s
		}
		m := &oracle.Model{DefaultAction: "deny"}
		for _, id := range order {
			m.Rules = append(m.Rules, byID[id])
		}
		return m
	}

	srcs := oracle.Addrs("ipv4", netA)
	dsts := oracle.Addrs("ipv4", netB)
	u := oracle.Universe{Family: "ipv4", Protos: []int{6}, SrcAddrs: srcs, DstAddrs: dsts,
		SrcPorts: []int{0}, DstPorts: []int{79, 80, 84, 85, 86, 90, 91}}
	pks := oracle.Enumerate(u)

	// Order A: wide first -> narrow fully shadowed (wide allows everything
	// narrow would deny).
	crA := submit(t, h, mk([]string{"wide", "narrow"}))
	assertDiagnostics(t, crA, mk([]string{"wide", "narrow"}), map[string][]oracle.Pk{"ipv4": pks})
	if !hasDiag(crA, "narrow", "ipv4", "fully_shadowed") {
		t.Fatalf("expected narrow fully shadowed when wide is first: %+v", crA.Report.Diagnostics)
	}
	if hasDiag(crA, "wide", "ipv4", "fully_shadowed") || hasDiag(crA, "wide", "ipv4", "partially_shadowed") {
		t.Fatalf("wide must be clean when first")
	}

	// Order B: narrow first -> narrow live; wide partially shadowed on 85.
	crB := submit(t, h, mk([]string{"narrow", "wide"}))
	assertDiagnostics(t, crB, mk([]string{"narrow", "wide"}), map[string][]oracle.Pk{"ipv4": pks})
	if hasDiag(crB, "narrow", "ipv4", "fully_shadowed") || hasDiag(crB, "narrow", "ipv4", "partially_shadowed") {
		t.Fatalf("narrow must be clean when first")
	}
	if !hasDiag(crB, "wide", "ipv4", "partially_shadowed") {
		t.Fatalf("wide must be partially shadowed when narrow is first: %+v", crB.Report.Diagnostics)
	}

	// The swap flips the decision on the single crossing port 85: order A
	// allows it (wide wins), order B denies it (narrow wins). Verify against
	// the currently-loaded order A, then switch and verify B.
	pk85 := oracle.Pk{Family: "ipv4", Proto: 6, Src: srcs[0], Dst: dsts[0], SrcPort: 0, DstPort: 85}
	// crB was already submitted above; explicitly reload A then B so each
	// evaluation runs against the version it names.
	submit(t, h, mk([]string{"wide", "narrow"}))
	if oracleA := mk([]string{"wide", "narrow"}).Decide(pk85).Action; oracleA != "allow" {
		t.Fatalf("oracle order A should allow :85, got %s", oracleA)
	}
	if dA := evalPacket(t, h, pk85); dA.Decision != "allow" {
		t.Fatalf("order A should allow :85, got %s", dA.Decision)
	}
	submit(t, h, mk([]string{"narrow", "wide"}))
	if oracleB := mk([]string{"narrow", "wide"}).Decide(pk85).Action; oracleB != "deny" {
		t.Fatalf("oracle order B should deny :85, got %s", oracleB)
	}
	if dB := evalPacket(t, h, pk85); dB.Decision != "deny" {
		t.Fatalf("order B should deny :85, got %s", dB.Decision)
	}
}

// Scenario 3: default deny with no rules — everything denied, no diags.
func TestScenario3_DefaultDenyEmpty(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)
	m := &oracle.Model{DefaultAction: "deny"}
	cr := submit(t, h, m)
	if !cr.ParseOK || len(cr.Report.Diagnostics) != 0 {
		t.Fatalf("empty config should be clean, got %+v", cr)
	}
	pks := oracle.Enumerate(oracle.Universe{Family: "ipv4", Protos: []int{6},
		SrcAddrs: []string{"10.0.0.1"}, DstAddrs: []string{"10.0.0.5"},
		SrcPorts: []int{1234}, DstPorts: []int{80}})
	assertExhaustive(t, h, m, pks)
	d := evalPacket(t, h, pks[0])
	if d.Decision != "deny" || d.DecidedBy != "default" {
		t.Fatalf("expected default deny, got %s/%s", d.Decision, d.DecidedBy)
	}
}

// Scenario 4: default allow. A same-action allow rule early is redundant; a
// deny rule that protects traffic is live. Deletion invariant checked.
func TestScenario4_DefaultAllow(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	m := &oracle.Model{
		DefaultAction: "allow",
		Rules: []oracle.Spec{
			// redundant: dport 443 is already allowed by the default and no
			// later rule carves it out.
			{ID: "a1", Action: "allow", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 443, Hi: 443}},
			// load-bearing: carves out a denial over 22-23; no later rule
			// covers 22, so deleting it flips 22 back to default allow.
			{ID: "d1", Action: "deny", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 22, Hi: 23}},
			// fully shadowed: 23 is already denied by d1.
			{ID: "d2", Action: "deny", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 23, Hi: 23}},
			// partially shadowed: 23 decided by d1; 24 is its live remainder.
			{ID: "d3", Action: "deny", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 23, Hi: 24}},
		},
	}
	cr := submit(t, h, m)
	if !cr.ParseOK {
		t.Fatalf("parse: %+v", cr.ParseErrors)
	}
	srcs := oracle.Addrs("ipv4", netA)
	dsts := oracle.Addrs("ipv4", netB)
	pks := oracle.Enumerate(oracle.Universe{Family: "ipv4", Protos: []int{6},
		SrcAddrs: srcs, DstAddrs: dsts, SrcPorts: []int{0}, DstPorts: []int{21, 22, 23, 24, 443, 80}})
	assertExhaustive(t, h, m, pks)
	assertDiagnostics(t, cr, m, map[string][]oracle.Pk{"ipv4": pks})
	if !hasDiag(cr, "a1", "ipv4", "redundant") {
		t.Fatalf("a1 should be redundant with default allow")
	}
	if !hasDiag(cr, "d2", "ipv4", "fully_shadowed") {
		t.Fatalf("d2 should be fully shadowed by d1")
	}
	if !hasDiag(cr, "d3", "ipv4", "partially_shadowed") {
		t.Fatalf("d3 should be partially shadowed on port 22")
	}
	if hasDiag(cr, "d1", "ipv4", "redundant") {
		t.Fatalf("d1 is the only effective deny on :22 and must NOT be redundant")
	}
	assertDeletionInvariance(t, h, m, map[string][]oracle.Pk{"ipv4": pks}, cr)
}

// Scenario 5: IPv6 modeled independently. An IPv4 rule must not shadow an
// IPv6 rule, and vice versa.
func TestScenario5_FamiliesIndependent(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	m := &oracle.Model{
		DefaultAction: "deny",
		Rules: []oracle.Spec{
			{ID: "v4wide", Action: "allow", Family: "ipv4", Proto: "tcp",
				SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 90}},
			{ID: "v4narrow", Action: "deny", Family: "ipv4", Proto: "tcp",
				SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 85, Hi: 85}},
			{ID: "v6wide", Action: "allow", Family: "ipv6", Proto: "tcp",
				SrcCIDRs: []string{netA6}, DstCIDRs: []string{netB6},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 90}},
			{ID: "v6narrow", Action: "deny", Family: "ipv6", Proto: "tcp",
				SrcCIDRs: []string{netA6}, DstCIDRs: []string{netB6},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 85, Hi: 85}},
		},
	}
	cr := submit(t, h, m)
	if !cr.ParseOK {
		t.Fatalf("parse: %+v", cr.ParseErrors)
	}
	v4pks := oracle.Enumerate(oracle.Universe{Family: "ipv4", Protos: []int{6},
		SrcAddrs: oracle.Addrs("ipv4", netA), DstAddrs: oracle.Addrs("ipv4", netB),
		SrcPorts: []int{0}, DstPorts: []int{80, 85, 90}})
	v6pks := oracle.Enumerate(oracle.Universe{Family: "ipv6", Protos: []int{6},
		SrcAddrs: oracle.Addrs("ipv6", netA6), DstAddrs: oracle.Addrs("ipv6", netB6),
		SrcPorts: []int{0}, DstPorts: []int{80, 85, 90}})
	assertExhaustive(t, h, m, append(append([]oracle.Pk{}, v4pks...), v6pks...))
	assertDiagnostics(t, cr, m, map[string][]oracle.Pk{"ipv4": v4pks, "ipv6": v6pks})
	// v4narrow fully shadowed in ipv4; v6narrow likewise in ipv6, but no
	// cross-family shadowing.
	if !hasDiag(cr, "v4narrow", "ipv4", "fully_shadowed") {
		t.Fatalf("v4narrow should be fully shadowed (ipv4)")
	}
	if !hasDiag(cr, "v6narrow", "ipv6", "fully_shadowed") {
		t.Fatalf("v6narrow should be fully shadowed (ipv6)")
	}
	for _, d := range cr.Report.Diagnostics {
		if d.Witness != nil {
			// Witness must live in the diagnostic family.
			is6 := strings.Contains(d.Witness.SourceAddress, ":")
			if d.Family == "ipv4" && is6 {
				t.Fatalf("ipv4 diagnostic has ipv6 witness: %+v", d.Witness)
			}
			if d.Family == "ipv6" && !is6 {
				t.Fatalf("ipv6 diagnostic has ipv4 witness: %+v", d.Witness)
			}
		}
	}
}

// Scenario 6: configuration failures classify into stable error categories.
func TestScenario6_ParseErrorCategories(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	cases := []struct {
		name       string
		raw        map[string]any
		wantCats   []string
		wantRuleID string
	}{
		{"unknown protocol", map[string]any{
			"default_action": "deny",
			"rules": []any{map[string]any{
				"id": "x", "action": "allow", "protocol": "foobar",
				"source": "*", "destination": "*"}},
		}, []string{"unknown_protocol"}, "x"},
		{"invalid cidr", map[string]any{
			"default_action": "deny",
			"rules": []any{map[string]any{
				"id": "y", "action": "allow", "protocol": "tcp",
				"source": "not-a-cidr", "destination": "*"}},
		}, []string{"invalid_cidr"}, "y"},
		{"inverted ports", map[string]any{
			"default_action": "deny",
			"rules": []any{map[string]any{
				"id": "z", "action": "allow", "protocol": "tcp",
				"source": "*", "destination": "*", "destination_ports": "100-50"}},
		}, []string{"invalid_port"}, "z"},
		{"mixed families", map[string]any{
			"default_action": "deny",
			"rules": []any{map[string]any{
				"id": "m", "action": "allow", "protocol": "tcp",
				"source": netA, "destination": netB6}},
		}, []string{"mixed_address_family"}, "m"},
		{"icmp on ipv6 cidr", map[string]any{
			"default_action": "deny",
			"rules": []any{map[string]any{
				"id": "i", "action": "allow", "protocol": "icmp",
				"source": netA6, "destination": "*"}},
		}, []string{"protocol_family_mismatch"}, "i"},
		{"bad action", map[string]any{
			"default_action": "deny",
			"rules": []any{map[string]any{
				"id": "b", "action": "permit", "protocol": "tcp",
				"source": "*", "destination": "*"}},
		}, []string{"invalid_action"}, "b"},
		{"icmp with port", map[string]any{
			"default_action": "deny",
			"rules": []any{map[string]any{
				"id": "p", "action": "allow", "protocol": "icmp",
				"source": "*", "destination": "*", "destination_ports": "80"}},
		}, []string{"port_not_applicable"}, "p"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b, _ := json.Marshal(tc.raw)
			code, data := harness.PostRaw(t, h.BaseURL+"/configs", b)
			if code != http.StatusOK {
				t.Fatalf("status %d: %s", code, data)
			}
			var cr configResponse
			if err := json.Unmarshal(data, &cr); err != nil {
				t.Fatal(err)
			}
			if cr.ParseOK {
				t.Fatalf("expected parse failure for %s", tc.name)
			}
			var cats []string
			for _, e := range cr.ParseErrors {
				if e.RuleID == tc.wantRuleID {
					cats = append(cats, e.Category)
				}
			}
			for _, want := range tc.wantCats {
				if !containsStr(cats, want) {
					t.Fatalf("rule %s: want category %q, got categories %v (all errors: %+v)",
						tc.wantRuleID, want, cats, cr.ParseErrors)
				}
			}
		})
	}

	// Malformed JSON is an invalid_config hard failure.
	code, data := harness.PostRaw(t, h.BaseURL+"/configs", []byte("{not json"))
	if code != http.StatusOK {
		t.Fatalf("status %d: %s", code, data)
	}
	var cr configResponse
	json.Unmarshal(data, &cr)
	if cr.ParseOK || !containsStr(categories(cr), "invalid_config") {
		t.Fatalf("malformed JSON must yield invalid_config, got %+v", cr.ParseErrors)
	}

	// A failed config must not become the evaluation version: evaluation
	// without a valid config yields 409, not a crash.
	resp, err := http.Post(h.BaseURL+"/evaluate", "application/json",
		strings.NewReader(`{"family":"ipv4","protocol":"tcp","source_address":"1.1.1.1","destination_address":"2.2.2.2"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("evaluate with no valid config: want 409, got %d", resp.StatusCode)
	}
}

// Scenario 7: unknown numeric protocol is explicitly handled, not rejected:
// matching works and an uncertainty is surfaced.
func TestScenario7_UnknownNumericProtocol(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	m := &oracle.Model{
		DefaultAction: "deny",
		Rules: []oracle.Spec{
			{ID: "u1", Action: "allow", Proto: "99", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: fullPorts()},
		},
	}
	cr := submit(t, h, m)
	if !cr.ParseOK {
		t.Fatalf("numeric protocol should parse: %+v", cr.ParseErrors)
	}
	foundNote := false
	for _, n := range cr.Notes {
		if n.RuleID == "u1" && strings.Contains(n.Note, "uncertain") {
			foundNote = true
		}
	}
	if !foundNote {
		t.Fatalf("expected uncertainty note for numeric protocol 99, got %+v", cr.Notes)
	}
	pks := oracle.Enumerate(oracle.Universe{Family: "ipv4", Protos: []int{99, 6},
		SrcAddrs: oracle.Addrs("ipv4", netA), DstAddrs: oracle.Addrs("ipv4", netB),
		SrcPorts: []int{0}, DstPorts: []int{80}})
	assertExhaustive(t, h, m, pks)
	d := evalPacket(t, h, pks[0])
	if d.Decision != "allow" {
		t.Fatalf("proto 99 packet should match u1, got %s", d.Decision)
	}
	if len(d.Uncertainties) == 0 {
		t.Fatalf("expected uncertainty in decision for proto 99")
	}
}

// Scenario 8: protocol "any" with restricted ports — port-bearing protocols
// constrained; non-port protocols still match. Exhaustive check.
func TestScenario8_AnyProtocolPortSemantics(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	m := &oracle.Model{
		DefaultAction: "deny",
		Rules: []oracle.Spec{
			{ID: "web", Action: "allow", Proto: "any", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 80}},
		},
	}
	cr := submit(t, h, m)
	if !cr.ParseOK {
		t.Fatalf("parse: %+v", cr.ParseErrors)
	}
	// Config note must explain the subtle semantics.
	found := false
	for _, n := range cr.Notes {
		if strings.Contains(n.Note, "port-bearing") {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected a note explaining any+ports semantics")
	}
	pks := oracle.Enumerate(oracle.Universe{
		Family: "ipv4", Protos: []int{6, 17, 132, 1, 99},
		SrcAddrs: oracle.Addrs("ipv4", netA), DstAddrs: oracle.Addrs("ipv4", netB),
		SrcPorts: []int{0, 1000}, DstPorts: []int{80, 81},
	})
	assertExhaustive(t, h, m, pks)

	// Concrete probes: tcp/80 allow; tcp/81 deny; icmp (non-port) allow even
	// with dst port 81; udp/80 allow.
	inA := oracle.Addrs("ipv4", netA)[0]
	inB := oracle.Addrs("ipv4", netB)[0]
	probes := []struct {
		proto int
		dp    int
		want  string
	}{
		{6, 80, "allow"}, {6, 81, "deny"}, {17, 80, "allow"},
		{132, 81, "deny"}, {1, 81, "allow"}, {99, 81, "allow"},
	}
	for _, pr := range probes {
		d := evalPacket(t, h, oracle.Pk{Family: "ipv4", Proto: pr.proto, Src: inA, Dst: inB, SrcPort: 0, DstPort: pr.dp})
		if d.Decision != pr.want {
			t.Fatalf("proto=%d dp=%d: want %s got %s", pr.proto, pr.dp, pr.want, d.Decision)
		}
	}
}

// --- small assertions ---

func hasDiag(cr configResponse, rule, family, kind string) bool {
	if cr.Report == nil {
		return false
	}
	for _, d := range cr.Report.Diagnostics {
		if d.RuleID == rule && d.Family == family && d.Kind == kind {
			return true
		}
	}
	return false
}

func assertContains(t *testing.T, xs []string, want string) {
	t.Helper()
	for _, x := range xs {
		if x == want {
			return
		}
	}
	t.Fatalf("expected %q in %v", want, xs)
}
func assertNotContains(t *testing.T, xs []string, want string) {
	t.Helper()
	for _, x := range xs {
		if x == want {
			t.Fatalf("did not expect %q in %v", want, xs)
		}
	}
}
func containsStr(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}
func categories(cr configResponse) []string {
	var out []string
	for _, e := range cr.ParseErrors {
		out = append(out, e.Category)
	}
	return out
}

var _ = fmt.Sprintf
