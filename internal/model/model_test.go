package model

import "testing"

func TestSummary_StableDigest(t *testing.T) {
	doc := map[string]any{
		"apiVersion": "v1",
		"kind":       "Widget",
		"metadata":   map[string]any{"name": "orders", "namespace": "shop"},
		"spec": map[string]any{
			"replicas": float64(2),
			"capacity": float64(200),
		},
	}
	s1, err := Summarize(doc)
	if err != nil {
		t.Fatal(err)
	}
	s2, err := Summarize(doc)
	if err != nil {
		t.Fatal(err)
	}
	if s1.Digest != s2.Digest {
		t.Fatalf("digest not stable: %s vs %s", s1.Digest, s2.Digest)
	}
	if s1.Replicas != 2 {
		t.Fatalf("replicas = %d, want 2", s1.Replicas)
	}
	// Integer normalization: 200 must not appear as 200.0 in canonical form.
	b, err := Canonical(doc)
	if err != nil {
		t.Fatal(err)
	}
	if contains(string(b), "200.0") || contains(string(b), "2.0") {
		t.Fatalf("canonical JSON leaked float artifacts: %s", b)
	}
	// A semantic change changes the digest.
	doc["spec"].(map[string]any)["replicas"] = float64(3)
	s3, _ := Summarize(doc)
	if s3.Digest == s1.Digest {
		t.Fatalf("digest did not change after semantic edit")
	}
}

func TestRequestValidate(t *testing.T) {
	cases := []struct {
		name    string
		req     Request
		wantErr bool
	}{
		{"missing uid", Request{Operation: OpCreate, Object: map[string]any{}}, true},
		{"bad operation", Request{UID: "u", Operation: "PATCH", Object: map[string]any{}}, true},
		{"create missing object", Request{UID: "u", Operation: OpCreate}, true},
		{"create missing name", Request{UID: "u", Operation: OpCreate, Object: map[string]any{
			"apiVersion": "v1", "kind": "Widget",
			"metadata": map[string]any{},
		}}, true},
		{"valid create", Request{UID: "u", Operation: OpCreate, Object: map[string]any{
			"apiVersion": "v1", "kind": "Widget",
			"metadata": map[string]any{"name": "n"}, "spec": map[string]any{},
		}}, false},
		{"delete with old object", Request{UID: "u", Operation: OpDelete, OldObject: map[string]any{
			"apiVersion": "v1", "kind": "Widget",
			"metadata": map[string]any{"name": "n"},
		}}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.req.Validate()
			if (err != nil) != tc.wantErr {
				t.Fatalf("Validate() err=%v, wantErr=%v", err, tc.wantErr)
			}
		})
	}
}

func TestReason_CategoryAndStatus(t *testing.T) {
	cases := map[Reason]string{
		ReasonInvalidInput:      "input_error",
		ReasonStateConflict:     "state_conflict",
		ReasonResourceExhausted: "resource_exhausted",
		ReasonComputeFailure:    "compute_failure",
		ReasonTimeout:           "compute_failure",
		ReasonIllegalPath:       "compute_failure",
		ReasonValidationDenied:  "compute_failure",
	}
	for r, cat := range cases {
		if r.Category() != cat {
			t.Errorf("%s category = %s, want %s", r, r.Category(), cat)
		}
	}
	status := map[Reason]int{
		ReasonInvalidInput: 400, ReasonValidationDenied: 422,
		ReasonResourceExhausted: 429, ReasonStateConflict: 409,
		ReasonTimeout: 504, ReasonComputeFailure: 500, ReasonIllegalPath: 500,
	}
	for r, want := range status {
		if r.HTTPStatus() != want {
			t.Errorf("%s http = %d, want %d", r, r.HTTPStatus(), want)
		}
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
