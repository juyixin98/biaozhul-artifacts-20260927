package engine

import (
	"testing"

	"netpolicy/internal/domain"
)

// minimalSnap builds a two-namespace, two-endpoint snapshot for focused
// engine tests. Tests construct inputs by hand rather than deriving them
// from the engine, so expected values are independently authored.
func minimalSnap() *domain.Snapshot {
	return &domain.Snapshot{
		Revision: 7,
		Namespaces: []domain.Namespace{
			{Name: "a"}, {Name: "b"},
		},
		Endpoints: []domain.Endpoint{
			{UID: "u1", Name: "e1", Namespace: "a", Labels: map[string]string{"app": "x"},
				Ports: []domain.Port{{Name: "web", Number: 80, Protocol: domain.ProtocolTCP}}},
			{UID: "u2", Name: "e2", Namespace: "a", Labels: map[string]string{"app": "y"},
				Ports: []domain.Port{{Name: "web", Number: 8080, Protocol: domain.ProtocolTCP}}},
			{UID: "u3", Name: "e3", Namespace: "b", Labels: map[string]string{"app": "z"},
				Ports: []domain.Port{{Number: 80, Protocol: domain.ProtocolTCP}}},
		},
	}
}

func TestUnselectedWorkloadsDefaultAllowBothDirections(t *testing.T) {
	e := New(minimalSnap())
	d, err := e.Check(Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 80})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if d.Verdict != VerdictAllow || !d.Allowed {
		t.Fatalf("expected ALLOW for two unselected workloads, got %s (%s)", d.Verdict, d.Reason)
	}
	if d.Ingress.Isolated || d.Egress.Isolated {
		t.Fatalf("both sides must report unselected, got ingress=%v egress=%v", d.Ingress.Isolated, d.Egress.Isolated)
	}
	if d.Reason != ReasonIngressUnselected+"+"+ReasonEgressUnselected {
		t.Fatalf("unexpected reason %q", d.Reason)
	}
}

func TestBothSidesMustAllow(t *testing.T) {
	snap := minimalSnap()
	// Egress policy permits u1 -> u2, but no ingress policy on u2. Ingress
	// defaults allow, so the connection is allowed.
	snap.Policies = append(snap.Policies, domain.Policy{
		Name: "eg", Namespace: "a",
		PodSelector: domain.Selector{MatchLabels: map[string]string{"app": "x"}},
		PolicyTypes: []domain.PolicyType{domain.PolicyTypeEgress},
		Egress: []domain.EgressRule{{
			To:    []domain.Peer{{PodSelector: &domain.Selector{MatchLabels: map[string]string{"app": "y"}}}},
			Ports: []domain.RulePort{{Number: 80}},
		}},
	})
	e := New(snap)
	cases := []struct {
		name        string
		src, dst    string
		port        int
		wantVerdict Verdict
		wantReason  string
	}{
		{"egress allowed ingress default allowed", "u1", "u2", 80, VerdictAllow, ReasonIngressUnselected},
		{"reverse blocked by u2 egress not isolated? check", "u2", "u1", 80, VerdictAllow, ReasonIngressUnselected + "+" + ReasonEgressUnselected},
		{"wrong port blocked by egress", "u1", "u2", 8080, VerdictDeny, ReasonEgressDefaultDeny},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d, err := e.Check(Input{SourceUID: tc.src, DestUID: tc.dst, Protocol: domain.ProtocolTCP, Port: tc.port})
			if err != nil {
				t.Fatalf("err: %v", err)
			}
			if d.Verdict != tc.wantVerdict || d.Reason != tc.wantReason {
				t.Fatalf("got %s/%s want %s/%s", d.Verdict, d.Reason, tc.wantVerdict, tc.wantReason)
			}
		})
	}
}

func TestIngressAllowButEgressDeny(t *testing.T) {
	snap := minimalSnap()
	// u1 egress isolated with no rules; u2 ingress allows u1 on 80.
	snap.Policies = append(snap.Policies,
		domain.Policy{
			Name: "eg-deny", Namespace: "a",
			PodSelector: domain.Selector{MatchLabels: map[string]string{"app": "x"}},
			PolicyTypes: []domain.PolicyType{domain.PolicyTypeEgress},
		},
		domain.Policy{
			Name: "in-allow", Namespace: "a",
			PodSelector: domain.Selector{MatchLabels: map[string]string{"app": "y"}},
			PolicyTypes: []domain.PolicyType{domain.PolicyTypeIngress},
			Ingress: []domain.IngressRule{{
				From:  []domain.Peer{{PodSelector: &domain.Selector{MatchLabels: map[string]string{"app": "x"}}}},
				Ports: []domain.RulePort{{Number: 80}},
			}},
		},
	)
	e := New(snap)
	d, _ := e.Check(Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 80})
	if d.Verdict != VerdictDeny || d.Reason != ReasonEgressDefaultDeny {
		t.Fatalf("expected egress default deny, got %s/%s", d.Verdict, d.Reason)
	}
	if !d.Ingress.Allowed || len(d.Ingress.Matches) != 1 {
		t.Fatalf("ingress side must show the allow match, got %+v", d.Ingress)
	}
	if d.Egress.Allowed || !d.Egress.Isolated {
		t.Fatalf("egress side must be isolated with no match, got %+v", d.Egress)
	}
}

func TestNamedPortResolvesOnDestination(t *testing.T) {
	snap := minimalSnap()
	// Same name "web": 80 on u1, 8080 on u2. Policy allows u1 -> u2 by name.
	snap.Policies = append(snap.Policies,
		domain.Policy{
			Name: "eg", Namespace: "a",
			PodSelector: domain.Selector{MatchLabels: map[string]string{"app": "x"}},
			PolicyTypes: []domain.PolicyType{domain.PolicyTypeEgress},
			Egress: []domain.EgressRule{{
				To:    []domain.Peer{{PodSelector: &domain.Selector{MatchLabels: map[string]string{"app": "y"}}}},
				Ports: []domain.RulePort{{Name: "web"}},
			}},
		},
		domain.Policy{
			Name: "ing", Namespace: "a",
			PodSelector: domain.Selector{MatchLabels: map[string]string{"app": "y"}},
			PolicyTypes: []domain.PolicyType{domain.PolicyTypeIngress},
			Ingress: []domain.IngressRule{{
				From:  []domain.Peer{{PodSelector: &domain.Selector{MatchLabels: map[string]string{"app": "x"}}}},
				Ports: []domain.RulePort{{Name: "web"}},
			}},
		},
	)
	e := New(snap)
	d8080, _ := e.Check(Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 8080})
	if d8080.Verdict != VerdictAllow {
		t.Fatalf("named web on destination is 8080, expected ALLOW, got %s/%s", d8080.Verdict, d8080.Reason)
	}
	d80, _ := e.Check(Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 80})
	if d80.Verdict != VerdictDeny {
		t.Fatalf("named port must not resolve globally: 80 is u1's web, not u2's; got %s/%s", d80.Verdict, d80.Reason)
	}
}

func TestAmbiguousNamedPortIsUndecidable(t *testing.T) {
	snap := minimalSnap()
	snap.Endpoints[1].Ports = []domain.Port{
		{Name: "web", Number: 8080, Protocol: domain.ProtocolTCP},
		{Name: "web", Number: 9090, Protocol: domain.ProtocolTCP},
	}
	snap.Policies = append(snap.Policies, domain.Policy{
		Name: "ing", Namespace: "a",
		PodSelector: domain.Selector{MatchLabels: map[string]string{"app": "y"}},
		PolicyTypes: []domain.PolicyType{domain.PolicyTypeIngress},
		Ingress: []domain.IngressRule{{
			From:  []domain.Peer{{PodSelector: &domain.Selector{MatchLabels: map[string]string{"app": "x"}}}},
			Ports: []domain.RulePort{{Name: "web"}},
		}},
	})
	e := New(snap)
	d, _ := e.Check(Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 8080})
	if d.Verdict != VerdictUndecidable || d.Reason != ReasonNamedPortAmbiguous {
		t.Fatalf("expected undecidable ambiguity, got %s/%s", d.Verdict, d.Reason)
	}
	if len(d.Ingress.Ambiguities) == 0 {
		t.Fatalf("expected ambiguity details on ingress side, got none")
	}
}

func TestUnknownEndpointAndBadInputAreUndecidable(t *testing.T) {
	e := New(minimalSnap())
	cases := []struct {
		name   string
		in     Input
		reason string
	}{
		{"unknown source", Input{SourceUID: "ghost", DestUID: "u1", Protocol: domain.ProtocolTCP, Port: 80}, ReasonEndpointUnknown},
		{"unknown dest", Input{SourceUID: "u1", DestUID: "ghost", Protocol: domain.ProtocolTCP, Port: 80}, ReasonEndpointUnknown},
		{"bad protocol", Input{SourceUID: "u1", DestUID: "u2", Protocol: "SCTP", Port: 80}, ReasonProtocolUnsupported},
		{"port zero", Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 0}, ReasonPortOutOfRange},
		{"port huge", Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 70000}, ReasonPortOutOfRange},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d, err := e.Check(tc.in)
			if err != nil {
				t.Fatalf("err: %v", err)
			}
			if d.Verdict != VerdictUndecidable || d.Reason != tc.reason {
				t.Fatalf("got %s/%s want UNDECIDABLE/%s", d.Verdict, d.Reason, tc.reason)
			}
		})
	}
}

func TestRevisionPinMismatch(t *testing.T) {
	e := New(minimalSnap())
	d, _ := e.Check(Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 80, PinRevision: 6})
	if d.Verdict != VerdictUndecidable || d.Reason != ReasonRevisionConflict {
		t.Fatalf("expected revision conflict, got %s/%s", d.Verdict, d.Reason)
	}
	dOK, _ := e.Check(Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 80, PinRevision: 7})
	if dOK.Verdict != VerdictAllow {
		t.Fatalf("matching pin must evaluate, got %s", dOK.Verdict)
	}
}

func TestPolicySelectionScopedToPolicyNamespace(t *testing.T) {
	snap := minimalSnap()
	// A policy in namespace b with podSelector {} must NOT isolate u1/u2 in a.
	snap.Policies = append(snap.Policies, domain.Policy{
		Name: "b-wall", Namespace: "b",
		PodSelector: domain.Selector{},
		PolicyTypes: []domain.PolicyType{domain.PolicyTypeIngress, domain.PolicyTypeEgress},
	})
	e := New(snap)
	d, _ := e.Check(Input{SourceUID: "u1", DestUID: "u2", Protocol: domain.ProtocolTCP, Port: 80})
	if d.Verdict != VerdictAllow {
		t.Fatalf("empty podSelector must only apply in its own namespace, got %s/%s", d.Verdict, d.Reason)
	}
	d2, _ := e.Check(Input{SourceUID: "u1", DestUID: "u3", Protocol: domain.ProtocolTCP, Port: 80})
	if d2.Verdict != VerdictDeny || d2.Reason != ReasonIngressDefaultDeny {
		t.Fatalf("u3 should be ingress-isolated by b-wall, got %s/%s", d2.Verdict, d2.Reason)
	}
}
