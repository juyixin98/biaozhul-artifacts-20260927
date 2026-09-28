package patch

import (
	"errors"
	"testing"

	"admission/internal/model"
)

func TestApply_AllowedAddsAndNoOpRewrite(t *testing.T) {
	doc := map[string]any{
		"apiVersion": "v1",
		"kind":       "Widget",
		"metadata":   map[string]any{"name": "orders"},
		"spec":       map[string]any{"replicas": float64(1)},
	}
	declared := []string{"/spec/schedule", "/spec/capacity"}
	ops := []model.PatchOp{
		{Op: "add", Path: "/spec/schedule", Value: "always"},
		// value-equal rewrite must report changed=false (idempotency support)
		{Op: "add", Path: "/spec/capacity", Value: int64(100)},
		{Op: "add", Path: "/spec/capacity", Value: int64(100)},
	}
	changed, err := Apply(doc, ops, declared)
	if err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if !changed {
		t.Fatalf("expected changed=true on first capacity write")
	}
	spec := doc["spec"].(map[string]any)
	if spec["schedule"] != "always" {
		t.Fatalf("schedule not set: %#v", spec)
	}
	if spec["capacity"] != int64(100) {
		t.Fatalf("capacity = %#v", spec["capacity"])
	}

	// Re-apply identical set: fully converged.
	changed, err = Apply(doc, ops, declared)
	if err != nil {
		t.Fatalf("re-apply: %v", err)
	}
	if changed {
		t.Fatalf("re-apply of identical ops must be a no-op, got changed=true")
	}
}

func TestApply_RemoveMissingIsNoOp(t *testing.T) {
	doc := map[string]any{"spec": map[string]any{}}
	changed, err := Apply(doc, []model.PatchOp{{Op: "remove", Path: "/spec/absent"}}, []string{"/spec/absent"})
	if err != nil {
		t.Fatalf("remove absent: %v", err)
	}
	if changed {
		t.Fatalf("removing an absent key must not report a change")
	}
}

func TestApply_IllegalPathRejects(t *testing.T) {
	cases := []struct {
		name     string
		path     string
		declared []string
	}{
		{"protected name", "/metadata/name", []string{"/metadata/name"}},
		{"protected kind", "/kind", []string{"/kind"}},
		{"undeclared spec key", "/spec/evil", []string{"/spec/replicas"}},
		{"prefix cannot escape", "/metadata/labels/x", []string{"/metadata/annotations/*"}},
		{"exact is not prefix", "/spec/replicas2", []string{"/spec/replicas"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			doc := map[string]any{"metadata": map[string]any{"name": "n"}, "spec": map[string]any{}}
			ops := []model.PatchOp{{Op: "add", Path: tc.path, Value: "x"}}
			_, err := Apply(doc, ops, tc.declared)
			if !errors.Is(err, ErrIllegalPath) {
				t.Fatalf("want ErrIllegalPath, got %v", err)
			}
			// The document must not show the rejected write.
			if doc["metadata"].(map[string]any)["name"] != "n" {
				t.Fatalf("protected name mutated on rejected patch")
			}
		})
	}
}

func TestApply_RejectedSequenceLeavesCandidateUntouched(t *testing.T) {
	// First op legal, second illegal: caller passes a copy; on error the copy
	// is discarded, so the original object is exactly as it was.
	orig := map[string]any{"spec": map[string]any{"replicas": float64(1)}}
	work := DeepCopy(orig)
	ops := []model.PatchOp{
		{Op: "add", Path: "/spec/capacity", Value: int64(100)},
		{Op: "add", Path: "/spec/evil", Value: true},
	}
	_, err := Apply(work, ops, []string{"/spec/capacity", "/spec/evil-NO"})
	if err == nil {
		t.Fatalf("expected illegal path error")
	}
	// orig (the committed working object) is untouched.
	spec := orig["spec"].(map[string]any)
	if _, present := spec["capacity"]; present {
		t.Fatalf("original object saw a partial transform: %#v", orig)
	}
}

func TestAllowed_PrefixMatching(t *testing.T) {
	declared := []string{"/metadata/annotations/*"}
	for _, p := range []string{"/metadata/annotations/admission.uid", "/metadata/annotations/a/b"} {
		if err := Allowed(p, declared); err != nil {
			t.Fatalf("%s should be allowed: %v", p, err)
		}
	}
	for _, p := range []string{"/metadata/annotations", "/metadata/labels/x", "/spec/annotations/x"} {
		if err := Allowed(p, declared); err == nil {
			t.Fatalf("%s should be rejected", p)
		}
	}
}

func TestParse_Escaping(t *testing.T) {
	tokens, err := Parse("/metadata/annotations/a~1b~0c")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"metadata", "annotations", "a/b~c"}
	if len(tokens) != 3 || tokens[2] != want[2] {
		t.Fatalf("got %v, want %v", tokens, want)
	}
}
