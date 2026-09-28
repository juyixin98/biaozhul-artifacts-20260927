package domain

import "testing"

func TestSelectorSemantics(t *testing.T) {
	labels := map[string]string{"app": "web", "env": "prod", "tier": "fe"}
	cases := []struct {
		name string
		sel  *Selector
		want bool
	}{
		{"nil selector matches nothing", nil, false},
		{"empty selector matches all", &Selector{}, true},
		{"matchLabels equal", &Selector{MatchLabels: map[string]string{"app": "web"}}, true},
		{"matchLabels mismatch", &Selector{MatchLabels: map[string]string{"app": "db"}}, false},
		{"matchLabels extra required", &Selector{MatchLabels: map[string]string{"app": "web", "env": "prod"}}, true},
		{"matchLabels missing key", &Selector{MatchLabels: map[string]string{"team": "x"}}, false},
		{"In hit", &Selector{MatchExprs: []Requirement{{Key: "app", Operator: OpIn, Values: []string{"web", "db"}}}}, true},
		{"In miss", &Selector{MatchExprs: []Requirement{{Key: "app", Operator: OpIn, Values: []string{"db"}}}}, false},
		{"NotIn satisfied by other value", &Selector{MatchExprs: []Requirement{{Key: "app", Operator: OpNotIn, Values: []string{"db"}}}}, true},
		{"NotIn satisfied by missing key", &Selector{MatchExprs: []Requirement{{Key: "absent", Operator: OpNotIn, Values: []string{"x"}}}}, true},
		{"NotIn fails on listed value", &Selector{MatchExprs: []Requirement{{Key: "app", Operator: OpNotIn, Values: []string{"web"}}}}, false},
		{"Exists hit", &Selector{MatchExprs: []Requirement{{Key: "env", Operator: OpExists}}}, true},
		{"Exists miss", &Selector{MatchExprs: []Requirement{{Key: "ghost", Operator: OpExists}}}, false},
		{"DoesNotExist hit", &Selector{MatchExprs: []Requirement{{Key: "ghost", Operator: OpDoesNotExist}}}, true},
		{"DoesNotExist fail", &Selector{MatchExprs: []Requirement{{Key: "app", Operator: OpDoesNotExist}}}, false},
		{"combined all match", &Selector{
			MatchLabels: map[string]string{"app": "web"},
			MatchExprs:  []Requirement{{Key: "env", Operator: OpIn, Values: []string{"prod"}}},
		}, true},
		{"combined one fails", &Selector{
			MatchLabels: map[string]string{"app": "web"},
			MatchExprs:  []Requirement{{Key: "tier", Operator: OpIn, Values: []string{"be"}}},
		}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.sel.Matches(labels); got != tc.want {
				t.Fatalf("Matches = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestSelectorNilVsEmptyDistinction(t *testing.T) {
	var nilSel *Selector
	if nilSel.IsAbsent() != true {
		t.Fatal("nil selector must report absent")
	}
	if (&Selector{}).IsAbsent() != false {
		t.Fatal("non-nil empty selector must not report absent (it means match-all)")
	}
}

func TestPolicyTypeDefaulting(t *testing.T) {
	ingressOnly := Policy{Ingress: []IngressRule{{}}}
	pts := ingressOnly.EffectivePolicyTypes()
	if len(pts) != 1 || pts[0] != PolicyTypeIngress {
		t.Fatalf("ingress-only default = %v", pts)
	}
	both := Policy{Ingress: []IngressRule{{}}, Egress: []EgressRule{{}}}
	pts = both.EffectivePolicyTypes()
	if len(pts) != 2 || pts[0] != PolicyTypeIngress || pts[1] != PolicyTypeEgress {
		t.Fatalf("with egress rules default = %v", pts)
	}
	explicit := Policy{PolicyTypes: []PolicyType{PolicyTypeEgress}, Ingress: []IngressRule{{}}}
	pts = explicit.EffectivePolicyTypes()
	if len(pts) != 1 || pts[0] != PolicyTypeEgress {
		t.Fatalf("explicit policyTypes must not be defaulted, got %v", pts)
	}
}
