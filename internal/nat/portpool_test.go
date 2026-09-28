package nat

import "testing"

func TestPortPool_AllocateExhaustionAndReuse(t *testing.T) {
	p := NewPortPool(40000, 40002)
	var got []uint16
	for i := 0; i < 3; i++ {
		port, ok := p.Allocate()
		if !ok {
			t.Fatalf("allocation %d unexpectedly failed", i)
		}
		got = append(got, port)
	}
	if s := []uint16{40000, 40001, 40002}; !equalU16(got, s) {
		t.Fatalf("allocations = %v, want %v", got, s)
	}
	if _, ok := p.Allocate(); ok {
		t.Fatalf("allocation beyond pool size must fail")
	}
	// Release the middle port: reuse gives the lowest free port only.
	p.Release(40001)
	port, ok := p.Allocate()
	if !ok || port != 40001 {
		t.Fatalf("reuse = %d ok=%v, want 40001", port, ok)
	}
	if _, ok := p.Allocate(); ok {
		t.Fatalf("pool must be exhausted again after reusing the only hole")
	}
	if p.Size() != 3 {
		t.Fatalf("size = %d, want 3", p.Size())
	}
}

func TestPortPool_BurnThenAllocate(t *testing.T) {
	p := NewPortPool(40000, 40005)
	p.Burn(40002) // pretend an active mapping owns it
	port, ok := p.Allocate()
	if !ok || port != 40000 {
		t.Fatalf("after burn(40002), first allocate = %d ok=%v, want 40000", port, ok)
	}
	got := []uint16{port}
	for i := 0; i < 4; i++ {
		pr, ok := p.Allocate()
		if !ok {
			t.Fatalf("allocation %d failed; pool must skip burned port", i)
		}
		got = append(got, pr)
	}
	// Expect 40000,40001 then 40003,40004,40005 (40002 reserved).
	want := []uint16{40000, 40001, 40003, 40004, 40005}
	if !equalU16(got, want) {
		t.Fatalf("allocations = %v, want %v", got, want)
	}
	if _, ok := p.Allocate(); ok {
		t.Fatalf("pool exhausted expected")
	}
}

func equalU16(a, b []uint16) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
