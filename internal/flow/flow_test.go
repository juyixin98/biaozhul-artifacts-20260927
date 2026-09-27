package flow_test

import (
	"testing"

	"flowrouter/internal/apperr"
	"flowrouter/internal/flow"
	"flowrouter/internal/hashx"
)

func TestCanonicalKeyNormalizes(t *testing.T) {
	// Different textual spellings of the same IPv6 flow must produce one key.
	f1, err := flow.Parse("2001:db8::1", "2001:db8::2", 6, 1234, 80)
	if err != nil {
		t.Fatal(err)
	}
	f2, err := flow.Parse("2001:0db8:0000:0000:0000:0000:0000:0001",
		"2001:0db8::2", 6, 1234, 80)
	if err != nil {
		t.Fatal(err)
	}
	if f1.CanonicalKey() != f2.CanonicalKey() {
		t.Fatalf("keys differ:\n%s\n%s", f1.CanonicalKey(), f2.CanonicalKey())
	}
	if f1.Hash() != f2.Hash() {
		t.Fatal("normalized flows must hash identically")
	}
	want := "tcp|2001:db8::1:1234|2001:db8::2:80"
	if f1.CanonicalKey() != want {
		t.Fatalf("key=%q want %q", f1.CanonicalKey(), want)
	}
}

func TestTupleValidation(t *testing.T) {
	if _, err := flow.Parse("not-ip", "10.0.0.2", 6, 1, 2); err == nil {
		t.Fatal("bad src ip must fail")
	}
	if _, err := flow.Parse("10.0.0.1", "10.0.0.2", 6, 1, 2); err != nil {
		t.Fatalf("valid tuple rejected: %v", err)
	}
	if _, err := flow.Parse("10.0.0.1", "2001:db8::1", 6, 1, 2); err == nil {
		t.Fatal("mixed address families must fail")
	}
	// ports on a portless protocol are rejected
	if _, err := flow.Parse("10.0.0.1", "10.0.0.2", 1, 0, 80); err == nil {
		t.Fatal("ICMP with dst port must fail")
	}
	if _, err := flow.Parse("10.0.0.1", "10.0.0.2", 1, 0, 0); err != nil {
		t.Fatalf("icmp with zero ports should be accepted: %v", err)
	}
}

func TestErrorKind(t *testing.T) {
	_, err := flow.Parse("999.1.1.1", "10.0.0.2", 6, 1, 2)
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindInvalidInput || ae.Code != "BAD_SRC_IP" {
		t.Fatalf("err=%v", err)
	}
}

func TestHashSpacesDisjoint(t *testing.T) {
	// Domain prefixes must separate flow and vnode key spaces; same hash with
	// different domain prefixes can coincidentally collide for crafted inputs,
	// but the prefix bytes must at minimum differ in the hashed material.
	h1 := hashx.FlowHash("tcp|1.2.3.4:1|5.6.7.8:2")
	h2 := hashx.VNodeHash("tcp|1.2.3.4:1", 0) // replica formatting differs anyway
	if h1 == h2 {
		t.Fatal("flow/vnode hash spaces collided on representative inputs")
	}
	// Determinism.
	if hashx.FlowHash("x") != hashx.FlowHash("x") {
		t.Fatal("hash not deterministic")
	}
	// splitmix is a bijection on these known points (its documented test
	// vectors) — pinning them catches an accidental hash change.
	cases := map[uint64]uint64{
		0:                  0xe220a8397b1dcdaf,
		1:                  0x910a2dec89025cc1,
		0xffffffffffffffff: 0xe4d971771b652c20,
	}
	for in, want := range cases {
		if got := hashx.SplitMix64(in); got != want {
			t.Errorf("splitmix64(%d)=%#x want %#x", in, got, want)
		}
	}
}
