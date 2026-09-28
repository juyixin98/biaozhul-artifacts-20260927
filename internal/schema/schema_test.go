package schema

import "testing"

func TestNewValidation(t *testing.T) {
	if _, err := New("k", map[string]ListDecl{
		"a": {Type: "bogus"},
	}); err == nil {
		t.Fatal("bogus list type must error")
	}
	if _, err := New("k", map[string]ListDecl{
		"a": {Type: ListMap},
	}); err == nil {
		t.Fatal("map list without key must error")
	}
	if _, err := New("k", map[string]ListDecl{
		"a": {Type: ListSet, KeyName: "x"},
	}); err == nil {
		t.Fatal("key on a set list must error")
	}
	sc, err := New("k", map[string]ListDecl{
		"a.b":   {Type: ListAtomic},
		"x.y.z": {Type: ListMap, KeyName: "id"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if d, ok := sc.Lookup([]string{"a", "b"}); !ok || d.Type != ListAtomic {
		t.Fatalf("lookup a.b failed: %+v %v", d, ok)
	}
	if d, ok := sc.Lookup([]string{"x", "y", "z"}); !ok || d.KeyName != "id" {
		t.Fatalf("nested map lookup failed: %+v", d)
	}
	if _, ok := sc.Lookup([]string{"a"}); ok {
		t.Fatal("partial path must not match")
	}
	var empty *Schema
	if _, ok := empty.Lookup([]string{"a"}); ok {
		t.Fatal("nil schema lookup must be false")
	}
}
