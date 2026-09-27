package seqnum

import "testing"

func TestCompareWindowOrder(t *testing.T) {
	cases := []struct {
		name string
		a, b uint32
		want int
	}{
		{"simple greater", 1000, 900, 1},
		{"simple less", 900, 1000, -1},
		{"equal", 42, 42, 0},
		{"wrap: small after large", 5, 0xFFFFFFF0, 1},
		{"wrap: large before small", 0xFFFFFFF0, 5, -1},
		{"exactly half space compares negative (RFC 793 asymmetry)", 0x80000000, 0, -1},
		{"just inside half space", 0x7FFFFFFF, 0, 1},
		{"just beyond half space", 0x80000001, 0, -1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Compare(tc.a, tc.b); got != tc.want {
				t.Fatalf("Compare(%#x,%#x)=%d want %d", tc.a, tc.b, got, tc.want)
			}
		})
	}
}

func TestSub(t *testing.T) {
	cases := []struct {
		a, b uint32
		want int64
	}{
		{10, 5, 5},
		{5, 10, -5},
		{5, 0xFFFFFFFE, 7}, // 5 - (-2) = 7 across wrap
		{0xFFFFFFFE, 5, -7},
		{0, 0xFFFFFFFF, 1},
	}
	for _, tc := range cases {
		if got := Sub(tc.a, tc.b); got != tc.want {
			t.Fatalf("Sub(%#x,%#x)=%d want %d", tc.a, tc.b, got, tc.want)
		}
	}
}

func TestAddWrap(t *testing.T) {
	if Add(0xFFFFFFFD, 4) != 1 {
		t.Fatal("Add must wrap modulo 2^32")
	}
	if Add(0, 10) != 10 {
		t.Fatal("Add basic failed")
	}
}

func TestAddPanicOnHugeOffset(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic for offset >= 2^32")
		}
	}()
	Add(0, Space)
}

func TestBetween(t *testing.T) {
	if !BetweenLeft(10, 10, 20) || BetweenLeft(20, 10, 20) {
		t.Fatal("BetweenLeft half-open semantics wrong")
	}
	if !BetweenRight(20, 10, 20) || BetweenRight(10, 10, 20) {
		t.Fatal("BetweenRight half-open semantics wrong")
	}
	if !BetweenLeft(5, 0xFFFFFFF0, 0x10) {
		t.Fatal("BetweenLeft must wrap")
	}
}

func TestExtend(t *testing.T) {
	cases := []struct {
		name       string
		seq        uint32
		hint, want uint64
	}{
		{"near zero", 10, 0, 10},
		{"near hint", 0x10, 0xFFFFFFF8, 0x100000010},
		{"wrap backwards", 0xFFFFFFF8, 0x100000002, 0xFFFFFFF8},
		{"zero seq with wrapped hint", 0, 1 << 32, 1 << 32},
		{"seq zero crossing boundary down", 0xFFFFFFFF, 1 << 32, 0xFFFFFFFF},
		{"seq one crossing boundary up", 1, 0xFFFFFFFF, 0x100000001},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Extend(tc.seq, tc.hint); got != tc.want {
				t.Fatalf("Extend(%#x,%d)=%d want %d", tc.seq, tc.hint, got, tc.want)
			}
		})
	}
}
