package hashx_test

import (
	"testing"

	"flowrouter/internal/hashx"
)

func TestKnownVectors(t *testing.T) {
	// splitmix finalizer vectors (documented Mix13 constants). These change
	// iff the hash version changes, which must be a deliberate ring-wide
	// migration (see package docs).
	sm := map[uint64]uint64{
		0:                  0xe220a8397b1dcdaf,
		1:                  0x910a2dec89025cc1,
		0xffffffffffffffff: 0xe4d971771b652c20,
	}
	for in, want := range sm {
		if got := hashx.SplitMix64(in); got != want {
			t.Errorf("SplitMix64(%#x)=%#x want %#x", in, got, want)
		}
	}
}

func TestFNVReferenceVector(t *testing.T) {
	// FNV-1a 64-bit known vector for the empty string and "foobar" per the
	// reference implementation (before the splitmix finalizer).
	if got := hashx.FNV1a64(nil); got != 0xcbf29ce484222325 {
		t.Errorf("fnv1a64(empty)=%#x", got)
	}
	if got := hashx.FNV1a64([]byte("foobar")); got != 0x85944171f73967e8 {
		t.Errorf("fnv1a64(foobar)=%#x", got)
	}
}

func TestDomainPrefixesAndDeterminism(t *testing.T) {
	keys := []string{"a", "tcp|1.2.3.4:1|5.6.7.8:2", "vnode-v1|x#0"}
	for _, k := range keys {
		if hashx.Hash64([]byte(k)) != hashx.Hash64([]byte(k)) {
			t.Fatal("not deterministic")
		}
	}
	// identical inner material under different domain prefixes must differ
	inner := "same"
	if hashx.FlowHash(inner) == hashx.VNodeHash(inner, 0) {
		// they hash different prefix strings; collision would be suspicious
		t.Fatal("flow and vnode domain collided")
	}
	// vnode replicas spread across the circle (sanity avalanche check)
	seen := map[uint64]bool{}
	for r := 0; r < 100; r++ {
		h := hashx.VNodeHash("hop-a", r)
		if seen[h] {
			t.Fatalf("duplicate vnode hash at replica %d", r)
		}
		seen[h] = true
	}
}
