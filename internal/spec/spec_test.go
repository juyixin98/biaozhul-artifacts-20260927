package spec

import (
	"testing"

	"infraplanner/internal/model"
)

func TestParse_ValidStack(t *testing.T) {
	res := validStack()
	p, err := Parse(res)
	if err != nil {
		t.Fatalf("expected valid spec, got %v", err)
	}
	if len(p.Resources) != 4 {
		t.Fatalf("resources = %d, want 4", len(p.Resources))
	}
	deps := p.Dependencies(p.ByKey()[model.Key{Kind: model.KindInstance, Name: "api"}])
	if len(deps) != 1 || deps[0].Name != "web" {
		t.Fatalf("instance deps = %v, want [subnet/web]", deps)
	}
}

func TestParse_InputErrors(t *testing.T) {
	cases := []struct {
		name string
		res  []model.Desired
		want string // substring that must appear in the aggregated message
	}{
		{
			"unknown kind",
			[]model.Desired{{Kind: "ufo", Name: "x"}},
			"unknown kind",
		},
		{
			"missing required attr",
			[]model.Desired{{Kind: model.KindVPC, Name: "v", Attrs: map[string]string{"region": "r"}}},
			`missing required attribute "cidr"`,
		},
		{
			"unknown attribute",
			[]model.Desired{{Kind: model.KindVPC, Name: "v",
				Attrs: map[string]string{"cidr": "10/8", "region": "r", "bogus": "1"}}},
			`unknown attribute "bogus"`,
		},
		{
			"duplicate name",
			[]model.Desired{
				{Kind: model.KindVPC, Name: "v", Attrs: map[string]string{"cidr": "a", "region": "r"}},
				{Kind: model.KindVPC, Name: "v", Attrs: map[string]string{"cidr": "b", "region": "r"}},
			},
			"duplicate logical name",
		},
		{
			"dangling reference",
			[]model.Desired{
				{Kind: model.KindSubnet, Name: "s",
					Attrs: map[string]string{"cidr": "10.0/16"},
					Refs:  map[string]model.Ref{"network_ref": {Kind: model.KindVPC, Name: "ghost"}}},
			},
			"does not exist in the spec",
		},
		{
			"wrong reference kind",
			[]model.Desired{
				{Kind: model.KindBucket, Name: "b"},
				{Kind: model.KindSubnet, Name: "s",
					Attrs: map[string]string{"cidr": "10.0/16"},
					Refs:  map[string]model.Ref{"network_ref": {Kind: model.KindBucket, Name: "b"}}},
			},
			"must point at vpc",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Parse(tc.res)
			if err == nil {
				t.Fatal("expected input_error, got nil")
			}
			me, ok := model.AsError(err)
			if !ok || me.Category != model.CatInput || me.Code != "invalid_spec" {
				t.Fatalf("err = %v, want category=%s code=invalid_spec", err, model.CatInput)
			}
			if !contains(me.Message, tc.want) {
				t.Fatalf("message %q does not contain %q", me.Message, tc.want)
			}
		})
	}
}

func TestParse_SelfReferenceRejected(t *testing.T) {
	// A reference that points back at its own resource must be rejected as an
	// input error (here it is also wrong-kind; either way the declaration is
	// refused before planning). The supported schema's only ref edge
	// (subnet->vpc) cannot form a multi-node cycle, so self-reference is the
	// constructible cyclic case.
	self := []model.Desired{
		{Kind: model.KindSubnet, Name: "s",
			Attrs: map[string]string{"cidr": "10/16"},
			Refs:  map[string]model.Ref{"network_ref": {Kind: model.KindSubnet, Name: "s"}}},
	}
	_, err := Parse(self)
	if err == nil {
		t.Fatal("expected rejection")
	}
	me, _ := model.AsError(err)
	if me == nil || me.Category != model.CatInput {
		t.Fatalf("want input_error, got %v", err)
	}
}

func TestParse_AggregatesAllProblems(t *testing.T) {
	// Two independent errors must both be reported in one pass.
	res := []model.Desired{
		{Kind: model.KindVPC, Name: ""}, // invalid
		{Kind: "nope", Name: "z"},       // invalid
	}
	_, err := Parse(res)
	me, _ := model.AsError(err)
	if me == nil {
		t.Fatal("expected error")
	}
	if !contains(me.Message, "2 problem(s)") {
		t.Fatalf("want aggregated count in %q", me.Message)
	}
}

func validStack() []model.Desired {
	return []model.Desired{
		{Kind: model.KindInstance, Name: "api",
			Attrs: map[string]string{"image": "img-1", "shape": "small"},
			Refs:  map[string]model.Ref{"subnet_ref": {Kind: model.KindSubnet, Name: "web"}}},
		{Kind: model.KindVPC, Name: "main",
			Attrs: map[string]string{"cidr": "10.0.0.0/8", "region": "east"}},
		{Kind: model.KindSubnet, Name: "web",
			Attrs: map[string]string{"cidr": "10.1.0.0/16", "zone": "z1"},
			Refs:  map[string]model.Ref{"network_ref": {Kind: model.KindVPC, Name: "main"}}},
		{Kind: model.KindBucket, Name: "artifacts"},
	}
}

func contains(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
