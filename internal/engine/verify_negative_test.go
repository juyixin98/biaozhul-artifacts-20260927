package engine

import (
	"math/big"
	"strings"
	"testing"

	"cidrcov/internal/netmodel"
)

// These tests exercise the package-private verify() guard directly to prove
// it actually rejects defective covers — it is not a rubber stamp.
func TestVerifyRejectsDefects(t *testing.T) {
	one := big.NewInt(1)
	max32 := new(big.Int).Sub(new(big.Int).Lsh(one, 32), one)
	allowed := []netmodel.Interval{{Start: big.NewInt(0), End: max32}}

	pref := func(base int64, length int) netmodel.Prefix {
		return netmodel.Prefix{Base: big.NewInt(base), Len: length}
	}

	t.Run("unaligned prefix", func(t *testing.T) {
		// /24 at base 1 is not aligned (size 256 does not divide 1).
		bad := []netmodel.Prefix{pref(1, 24)}
		err := verify(32, allowed, nil, bad)
		if err == nil || !strings.Contains(err.Error(), "not aligned") {
			t.Fatalf("want not-aligned error, got %v", err)
		}
	})

	t.Run("escapes address space", func(t *testing.T) {
		// /0 must start at 0; start elsewhere runs past 2^32-1.
		bad := []netmodel.Prefix{pref(1, 0)}
		err := verify(32, allowed, nil, bad)
		if err == nil {
			t.Fatalf("expected escape error, got nil")
		}
	})

	t.Run("overlap", func(t *testing.T) {
		bad := []netmodel.Prefix{pref(0, 24), pref(0, 32)}
		err := verify(32, allowed, nil, bad)
		if err == nil || !strings.Contains(err.Error(), "overlap") {
			t.Fatalf("want overlap error, got %v", err)
		}
	})

	t.Run("extra address", func(t *testing.T) {
		// Target is just 10.0.0.0/24 but cover also claims an adjacent /24.
		target := []netmodel.Interval{{
			Start: big.NewInt(0x0a000000), End: big.NewInt(0x0a0000ff)}}
		bad := []netmodel.Prefix{pref(0x0a000000, 24), pref(0x0a000100, 24)}
		err := verify(32, target, nil, bad)
		if err == nil || !strings.Contains(err.Error(), "adds") {
			t.Fatalf("want extra-address error, got %v", err)
		}
	})

	t.Run("missing address", func(t *testing.T) {
		// Target /24 but cover only holds the lower half.
		target := []netmodel.Interval{{
			Start: big.NewInt(0x0a000000), End: big.NewInt(0x0a0000ff)}}
		bad := []netmodel.Prefix{pref(0x0a000000, 25)}
		err := verify(32, target, nil, bad)
		if err == nil || !strings.Contains(err.Error(), "misses") {
			t.Fatalf("want missing-address error, got %v", err)
		}
	})

	t.Run("mergeable siblings", func(t *testing.T) {
		// Target /23 built with two un-merged /24 siblings.
		target := []netmodel.Interval{{
			Start: big.NewInt(0x0a000000), End: big.NewInt(0x0a0001ff)}}
		bad := []netmodel.Prefix{pref(0x0a000000, 24), pref(0x0a000100, 24)}
		err := verify(32, target, nil, bad)
		if err == nil || !strings.Contains(err.Error(), "mergeable siblings") {
			t.Fatalf("want mergeable-siblings error, got %v", err)
		}
	})

	t.Run("valid single prefix passes", func(t *testing.T) {
		good := []netmodel.Prefix{pref(0x0a000000, 24)}
		target := []netmodel.Interval{{
			Start: big.NewInt(0x0a000000), End: big.NewInt(0x0a0000ff)}}
		if err := verify(32, target, nil, good); err != nil {
			t.Fatalf("valid prefix rejected: %v", err)
		}
	})
}
