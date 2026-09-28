package specutil

import "testing"

func TestHashStableAcrossKeyOrder(t *testing.T) {
	a := map[string]any{"replicas": 2.0, "image": "widget:1",
		"labels": map[string]any{"z": "1", "a": "2"}}
	b := map[string]any{"labels": map[string]any{"a": "2", "z": "1"},
		"image": "widget:1", "replicas": 2.0}
	if Hash(a) != Hash(b) {
		t.Fatalf("hash must be key-order independent:\n%x\n%x", Hash(a), Hash(b))
	}
}

func TestHashChangesWithContent(t *testing.T) {
	h1 := Hash(map[string]any{"replicas": 1.0})
	h2 := Hash(map[string]any{"replicas": 2.0})
	if h1 == h2 {
		t.Fatalf("distinct specs must have distinct hashes")
	}
}
