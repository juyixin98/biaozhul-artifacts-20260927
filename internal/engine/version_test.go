package engine

import (
	"testing"

	"netpolicy/internal/domain"
)

// TestLabelSnapshotMatchesPolicyVersion pins down the consistency boundary:
// the engine is built from one immutable snapshot, so relabeling endpoints in
// a newer revision must not change decisions made against the older engine.
func TestLabelSnapshotMatchesPolicyVersion(t *testing.T) {
	build := func(rev int64, srcLabel string) *domain.Snapshot {
		return &domain.Snapshot{
			Revision:   rev,
			SourceHash: string(rune('a' + rev)),
			Namespaces: []domain.Namespace{{Name: "ns"}},
			Endpoints: []domain.Endpoint{
				{UID: "u-src", Name: "src", Namespace: "ns", Labels: map[string]string{"app": srcLabel}},
				{UID: "u-dst", Name: "dst", Namespace: "ns", Labels: map[string]string{"app": "dst"},
					Ports: []domain.Port{{Number: 80, Protocol: domain.ProtocolTCP}}},
			},
			Policies: []domain.Policy{{
				Name: "allow-trusted", Namespace: "ns",
				PodSelector: domain.Selector{MatchLabels: map[string]string{"app": "dst"}},
				PolicyTypes: []domain.PolicyType{domain.PolicyTypeIngress},
				Ingress: []domain.IngressRule{{
					From:  []domain.Peer{{PodSelector: &domain.Selector{MatchLabels: map[string]string{"app": "trusted"}}}},
					Ports: []domain.RulePort{{Number: 80}},
				}},
			}},
		}
	}

	v1 := build(1, "trusted")
	v2 := build(2, "untrusted")
	e1, e2 := New(v1), New(v2)

	d1, _ := e1.Check(Input{SourceUID: "u-src", DestUID: "u-dst", Protocol: domain.ProtocolTCP, Port: 80})
	d2, _ := e2.Check(Input{SourceUID: "u-src", DestUID: "u-dst", Protocol: domain.ProtocolTCP, Port: 80})
	if d1.Verdict != VerdictAllow {
		t.Fatalf("v1 decision = %s/%s, want ALLOW", d1.Verdict, d1.Reason)
	}
	if d2.Verdict != VerdictDeny || d2.Reason != ReasonIngressDefaultDeny {
		t.Fatalf("v2 decision = %s/%s, want DENY/ingress default", d2.Verdict, d2.Reason)
	}
	// Re-evaluating against the v1 engine after v2 exists must still use the
	// v1 label snapshot: the old engine is unaffected by the new revision.
	d1Again, _ := e1.Check(Input{SourceUID: "u-src", DestUID: "u-dst", Protocol: domain.ProtocolTCP, Port: 80})
	if d1Again.Verdict != VerdictAllow || d1Again.Revision != 1 {
		t.Fatalf("stale-engine drift: %s rev %d", d1Again.Verdict, d1Again.Revision)
	}
	// Mutating the slice used to build v1 cannot reach the indexed engine.
	v1.Endpoints[0].Labels["app"] = "mutated-after-build"
	d1After, _ := e1.Check(Input{SourceUID: "u-src", DestUID: "u-dst", Protocol: domain.ProtocolTCP, Port: 80})
	if d1After.Verdict != VerdictAllow {
		t.Fatalf("engine must not read post-build label mutations, got %s/%s", d1After.Verdict, d1After.Reason)
	}
}
