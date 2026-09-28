package jsonpatch

import (
	"encoding/json"
	"testing"

	"admission/internal/types"
)

func doc(t *testing.T, o types.Object) map[string]any {
	t.Helper()
	d, err := DocumentOf(o)
	if err != nil {
		t.Fatalf("DocumentOf: %v", err)
	}
	return d
}

func TestParsePointer(t *testing.T) {
	cases := []struct {
		in      string
		tokens  []string
		wantErr bool
	}{
		{"/spec/replicas", []string{"spec", "replicas"}, false},
		{"/metadata/annotations/admission.example.com~1team", []string{"metadata", "annotations", "admission.example.com/team"}, false},
		{"spec/replicas", nil, true}, // missing leading slash
		{"/spec//x", nil, true},      // empty token
	}
	for _, c := range cases {
		p, err := Parse(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("Parse(%q) expected error, got %v", c.in, p.Tokens())
			}
			continue
		}
		if err != nil {
			t.Errorf("Parse(%q) unexpected error: %v", c.in, err)
			continue
		}
		got := p.Tokens()
		if len(got) != len(c.tokens) {
			t.Errorf("Parse(%q) tokens=%v want %v", c.in, got, c.tokens)
			continue
		}
		for i := range got {
			if got[i] != c.tokens[i] {
				t.Errorf("Parse(%q) token[%d]=%q want %q", c.in, i, got[i], c.tokens[i])
			}
		}
	}
}

func TestPointerEscapeRoundtrip(t *testing.T) {
	// Canonical escape roundtrip: '/' -> '~1', '~' -> '~0'.
	for _, in := range []string{
		"/metadata/annotations/admission.example.com~1team",
		"/metadata/annotations/key~0name",
		"/spec/replicas",
	} {
		p, err := Parse(in)
		if err != nil {
			t.Fatalf("Parse(%q): %v", in, err)
		}
		if p.String() != in {
			t.Errorf("roundtrip Parse(%q).String()=%q", in, p.String())
		}
	}
}

func TestIsBelow(t *testing.T) {
	p := mustParse(t, "/spec/extra/reservedCPU")
	if !p.IsBelow([]string{"/spec", "/metadata"}) {
		t.Error("expected /spec/extra/reservedCPU below /spec")
	}
	if p.IsBelow([]string{"/spec/replicas"}) {
		t.Error("expected /spec/extra/reservedCPU NOT below /spec/replicas")
	}
	if !mustParse(t, "/spec").IsBelow([]string{"/spec"}) {
		t.Error("exact prefix match should be allowed")
	}
}

func TestApplyAddReplaceRemove(t *testing.T) {
	r := 2
	o := types.Object{Spec: types.Spec{Replicas: &r}}
	d := doc(t, o)

	// add a brand new member
	nd, err := Apply(d, types.PatchOp{Op: types.OpAdd, Path: "/spec/cpu", Value: "500m"})
	if err != nil {
		t.Fatalf("add cpu: %v", err)
	}
	if nd.(map[string]any)["spec"].(map[string]any)["cpu"] != "500m" {
		t.Fatalf("cpu not added: %v", nd)
	}
	// original untouched
	if _, present := d["spec"].(map[string]any)["cpu"]; present {
		t.Fatal("Apply mutated input document")
	}

	// replace existing
	nd, err = Apply(nd, types.PatchOp{Op: types.OpReplace, Path: "/spec/replicas", Value: 5})
	if err != nil {
		t.Fatalf("replace: %v", err)
	}
	if toInt(nd.(map[string]any)["spec"].(map[string]any)["replicas"]) != 5 {
		t.Fatalf("replicas not replaced: %v", nd)
	}

	// replace missing member => MissingTargetError
	_, err = Apply(nd, types.PatchOp{Op: types.OpReplace, Path: "/spec/missing", Value: 1})
	if _, ok := err.(*MissingTargetError); !ok {
		t.Fatalf("expected MissingTargetError, got %T %v", err, err)
	}

	// remove
	nd, err = Apply(nd, types.PatchOp{Op: types.OpRemove, Path: "/spec/cpu"})
	if err != nil {
		t.Fatalf("remove: %v", err)
	}
	if _, present := nd.(map[string]any)["spec"].(map[string]any)["cpu"]; present {
		t.Fatal("cpu still present after remove")
	}
}

func TestApplyNestedParents(t *testing.T) {
	// With the extra container present (as the pipeline normalizes), adding a
	// member deep inside works.
	d := doc(t, types.Object{Spec: types.Spec{Extra: map[string]any{}}})
	nd, err := Apply(d, types.PatchOp{Op: types.OpAdd, Path: "/spec/extra/reservedCPU", Value: 500})
	if err != nil {
		t.Fatalf("nested add: %v", err)
	}
	if toInt(nd.(map[string]any)["spec"].(map[string]any)["extra"].(map[string]any)["reservedCPU"]) != 500 {
		t.Fatalf("nested member missing: %v", nd)
	}

	// With the container absent, the missing parent is a distinct error.
	d2 := doc(t, types.Object{Spec: types.Spec{}})
	delete(d2["spec"].(map[string]any), "extra")
	_, err = Apply(d2, types.PatchOp{Op: types.OpAdd, Path: "/spec/extra/reservedCPU", Value: 500})
	if _, ok := err.(*MissingParentError); !ok {
		t.Fatalf("expected MissingParentError, got %v", err)
	}
}

func TestApplyRejectsArrayIndex(t *testing.T) {
	d := doc(t, types.Object{})
	_, err := Apply(d, types.PatchOp{Op: types.OpAdd, Path: "/spec/0", Value: 1})
	if err == nil {
		t.Fatal("expected array-index rejection")
	}
}

func TestApplyRejectsRootAndBadOp(t *testing.T) {
	d := doc(t, types.Object{})
	if _, err := Apply(d, types.PatchOp{Op: types.OpAdd, Path: ""}); err == nil {
		t.Error("empty path must error")
	}
	if _, err := Apply(d, types.PatchOp{Op: "frobnicate", Path: "/spec/cpu", Value: 1}); err == nil {
		t.Error("unknown op must error")
	}
}

func TestObjectRoundtrip(t *testing.T) {
	r := 3
	src := types.Object{
		APIVersion: "apps.example.com/v1", Kind: "Workload",
		Metadata: types.Metadata{Namespace: "team-a", Name: "w1", Labels: map[string]string{"team": "a"}},
		Spec:     types.Spec{Replicas: &r, CPU: "1", Memory: "256Mi", Extra: map[string]any{"x": float64(7)}},
	}
	d := doc(t, src)
	var back types.Object
	if err := ObjectFrom(d, &back); err != nil {
		t.Fatalf("ObjectFrom: %v", err)
	}
	a, _ := json.Marshal(src)
	b, _ := json.Marshal(back)
	if string(a) != string(b) {
		t.Fatalf("roundtrip mismatch:\n%s\n%s", a, b)
	}
}

func mustParse(t *testing.T, s string) Pointer {
	t.Helper()
	p, err := Parse(s)
	if err != nil {
		t.Fatalf("Parse(%s): %v", s, err)
	}
	return p
}

func toInt(v any) int {
	n, ok := v.(float64)
	if !ok {
		return -1
	}
	return int(n)
}
