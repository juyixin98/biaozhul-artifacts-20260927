// Known-answer tests assert EXACT prefix lists for hand-computed cases and
// verify real-width IPv4/IPv6 extremes against the independent big-integer
// trie oracle (test/refmodel.BigTrieCover) — not against the implementation
// under test.
package engine_test

import (
	"math/big"
	"testing"

	"cidrcov/internal/engine"
	"cidrcov/internal/ipparse"
	"cidrcov/internal/netmodel"
	"cidrcov/test/refmodel"
)

// oracleCIDRs computes the expected canonical prefix list entirely via the
// independent big.Int trie reference, then renders it — so known-answer tests
// compare two independent implementations, not production against itself.
func oracleCIDRs(t *testing.T, kind string, width int, allow, exclude []string) []string {
	t.Helper()
	toIvs := func(es []string) []refmodel.BigInterval {
		var out []refmodel.BigInterval
		for _, e := range es {
			pe, _, err := ipparse.Parse(e)
			if err != nil {
				t.Fatalf("parse %s: %v", e, err)
			}
			if pe.Kind != kind {
				t.Fatalf("%s is %s, want %s", e, pe.Kind, kind)
			}
			out = append(out, refmodel.BigInterval{Lo: pe.Interval.Start, Hi: pe.Interval.End})
		}
		return out
	}
	got := refmodel.BigTrieCover(width, toIvs(allow), toIvs(exclude))
	out := make([]string, len(got))
	for i, p := range got {
		out[i] = ipparse.Format(kind, netmodel.Prefix{Base: p.Base, Len: p.Len})
	}
	return out
}

func cidrs(res *engine.Result) []string {
	out := make([]string, len(res.Prefixes))
	for i, p := range res.Prefixes {
		out[i] = p.CIDR
	}
	return out
}

func assertCIDRs(t *testing.T, res *engine.Result, wantV4, wantV6 []string) {
	t.Helper()
	var gotV4, gotV6 []string
	for _, p := range res.Prefixes {
		if p.Family == ipparse.KindV4 {
			gotV4 = append(gotV4, p.CIDR)
		} else {
			gotV6 = append(gotV6, p.CIDR)
		}
	}
	eq := func(a, b []string) bool {
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
	if !eq(gotV4, wantV4) {
		t.Fatalf("ipv4 prefixes mismatch:\n got %v\nwant %v", gotV4, wantV4)
	}
	if !eq(gotV6, wantV6) {
		t.Fatalf("ipv6 prefixes mismatch:\n got %v\nwant %v", gotV6, wantV6)
	}
}

// TestKnownAnswersV4 uses hand-derived exact lists.
func TestKnownAnswersV4(t *testing.T) {
	cases := []struct {
		name    string
		allow   []string
		exclude []string
		want    []string
	}{
		{
			name:  "single /24",
			allow: []string{"192.168.0.0/24"},
			want:  []string{"192.168.0.0/24"},
		},
		{
			name:  "adjacent /25 siblings merge to /24",
			allow: []string{"10.0.0.0/25", "10.0.0.128/25"},
			want:  []string{"10.0.0.0/24"},
		},
		{
			name:  "merge across three levels",
			allow: []string{"10.0.0.0/26", "10.0.0.64/26", "10.0.0.128/25"},
			want:  []string{"10.0.0.0/24"},
		},
		{
			name:  "non-adjacent do not merge",
			allow: []string{"10.0.0.0/25", "10.0.1.0/25"},
			want:  []string{"10.0.0.0/25", "10.0.1.0/25"},
		},
		{
			name:  "overlapping collapse",
			allow: []string{"10.0.0.0/24", "10.0.0.128/25"},
			want:  []string{"10.0.0.0/24"},
		},
		{
			name:  "host bits canonicalized",
			allow: []string{"10.0.0.5/24"},
			want:  []string{"10.0.0.0/24"},
		},
		{
			name:  "bare address is /32",
			allow: []string{"10.0.0.1"},
			want:  []string{"10.0.0.1/32"},
		},
		{
			name:    "punch one host out of /24",
			allow:   []string{"10.0.0.0/24"},
			exclude: []string{"10.0.0.100/32"},
			want: []string{
				"10.0.0.0/26", "10.0.0.64/27", "10.0.0.96/30", "10.0.0.101/32",
				"10.0.0.102/31", "10.0.0.104/29", "10.0.0.112/28", "10.0.0.128/25",
			},
		},
		{
			name:    "punch network and broadcast addresses (they are coverable)",
			allow:   []string{"192.168.1.0/24"},
			exclude: []string{"192.168.1.0/32", "192.168.1.255/32"},
			want: []string{
				"192.168.1.1/32", "192.168.1.2/31", "192.168.1.4/30",
				"192.168.1.8/29", "192.168.1.16/28", "192.168.1.32/27",
				"192.168.1.64/26", "192.168.1.128/26", "192.168.1.192/27",
				"192.168.1.224/28", "192.168.1.240/29", "192.168.1.248/30",
				"192.168.1.252/31", "192.168.1.254/32",
			},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res := engine.Compute(c.allow, c.exclude, engine.Options{})
			if res.Status == "error" {
				t.Fatalf("unexpected failures: %+v", res.Failures)
			}
			// For the cases whose expected list is structurally non-trivial,
			// the hand list is still asserted; the reference oracle backs it
			// up in TestV4AgainstBigTrie below.
			assertCIDRs(t, res, c.want, nil)
		})
	}
}

// TestV4AgainstBigTrie checks varied IPv4 cases (including punching /8 out of
// the full space) against the independent reference instead of hand math.
func TestV4AgainstBigTrie(t *testing.T) {
	cases := []struct {
		name    string
		allow   []string
		exclude []string
	}{
		{"exclude whole /8 middle run", []string{"0.0.0.0/0"}, []string{"10.0.0.0/8"}},
		{"punch one host /24", []string{"10.0.0.0/24"}, []string{"10.0.0.100/32"}},
		{"punch both ends /24", []string{"192.168.1.0/24"},
			[]string{"192.168.1.0/32", "192.168.1.255/32"}},
		{"many excludes", []string{"10.0.0.0/16"},
			[]string{"10.0.1.0/24", "10.0.2.0/24", "10.0.10.0/24"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			want := oracleCIDRs(t, ipparse.KindV4, 32, c.allow, c.exclude)
			res := engine.Compute(c.allow, c.exclude, engine.Options{})
			if res.Status == "error" {
				t.Fatalf("failures: %+v", res.Failures)
			}
			assertCIDRs(t, res, want, nil)
		})
	}
}

// TestFullV4MinusOneHost checks the classic 32-prefix shape for 0.0.0.0/0
// minus one host, asserting count plus first/last and total covered.
func TestFullV4MinusOneHost(t *testing.T) {
	res := engine.Compute([]string{"0.0.0.0/0"}, []string{"0.0.0.0/32"}, engine.Options{})
	if res.Status == "error" {
		t.Fatalf("failures: %+v", res.Failures)
	}
	if got := len(res.Prefixes); got != 32 {
		t.Fatalf("expected 32 prefixes, got %d", got)
	}
	wantFirst, wantLast := "0.0.0.1/32", "128.0.0.0/1"
	if res.Prefixes[0].CIDR != wantFirst {
		t.Errorf("first = %s, want %s", res.Prefixes[0].CIDR, wantFirst)
	}
	if res.Prefixes[31].CIDR != wantLast {
		t.Errorf("last = %s, want %s", res.Prefixes[31].CIDR, wantLast)
	}
	if res.TotalCovered != "4294967295" {
		t.Errorf("covered = %s, want 4294967295", res.TotalCovered)
	}
}

// TestKnownAnswersV6 asserts exact text for hand-picked IPv6 cases plus a
// reference-backed check of the full-space-minus-lowest decomposition.
func TestKnownAnswersV6(t *testing.T) {
	t.Run("full v6", func(t *testing.T) {
		res := engine.Compute([]string{"::/0"}, nil, engine.Options{})
		assertCIDRs(t, res, nil, []string{"::/0"})
	})
	t.Run("single /64", func(t *testing.T) {
		res := engine.Compute([]string{"2001:db8::/64"}, nil, engine.Options{})
		assertCIDRs(t, res, nil, []string{"2001:db8::/64"})
	})
	t.Run("merge two /65 siblings", func(t *testing.T) {
		res := engine.Compute([]string{"2001:db8::/65", "2001:db8::8000:0:0:0/65"}, nil, engine.Options{})
		assertCIDRs(t, res, nil, []string{"2001:db8::/64"})
	})
	t.Run("punch lowest address of full space", func(t *testing.T) {
		want := oracleCIDRs(t, ipparse.KindV6, 128, []string{"::/0"}, []string{"::/128"})
		if len(want) != 128 {
			t.Fatalf("reference must yield 128 prefixes, got %d", len(want))
		}
		if want[0] != "::1/128" || want[127] != "8000::/1" {
			t.Fatalf("endpoints %s .. %s unexpected", want[0], want[127])
		}
		res := engine.Compute([]string{"::/0"}, []string{"::/128"}, engine.Options{})
		assertCIDRs(t, res, nil, want)
	})
}

// TestIPv6ExtremesAgainstBigTrie verifies real 128-bit edge cases against the
// independent structural big.Int trie oracle. It never enumerates addresses.
func TestIPv6ExtremesAgainstBigTrie(t *testing.T) {
	one := big.NewInt(1)
	max128 := new(big.Int).Sub(new(big.Int).Lsh(one, 128), one)

	toBigIvs := func(entries []string) []refmodel.BigInterval {
		var out []refmodel.BigInterval
		for _, e := range entries {
			pe, _, err := ipparse.Parse(e)
			if err != nil {
				t.Fatalf("parse %s: %v", e, err)
			}
			out = append(out, refmodel.BigInterval{Lo: pe.Interval.Start, Hi: pe.Interval.End})
		}
		return out
	}

	cases := []struct {
		name    string
		allow   []string
		exclude []string
	}{
		{"full space", []string{"::/0"}, nil},
		{"empty", nil, nil},
		{"punch lowest", []string{"::/0"}, []string{"::/128"}},
		{"punch highest", []string{"::/0"}, []string{"ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff/128"}},
		{"punch both ends", []string{"::/0"},
			[]string{"::/128", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff/128"}},
		{"exclude /127 at extreme top", []string{"::/0"},
			[]string{"ffff:ffff:ffff:ffff:ffff:ffff:ffff:fffe/127"}},
		{"two far apart hosts", []string{"::1/128", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff/128"}, nil},
		{"allow interval minus interior /64",
			[]string{"2001:db8::/32"}, []string{"2001:db8:1::/48"}},
		{"exclude spans entire allow", []string{"2001:db8::/64"}, []string{"2001:db8::/64"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res := engine.Compute(c.allow, c.exclude, engine.Options{})
			if res.Status == "error" {
				t.Fatalf("failures: %+v", res.Failures)
			}
			want := refmodel.BigTrieCover(128, toBigIvs(c.allow), toBigIvs(c.exclude))
			var got []netmodel.Prefix
			for _, p := range res.Prefixes {
				if p.Family == ipparse.KindV6 {
					base, ok := new(big.Int).SetString(p.Base, 10)
					if !ok {
						t.Fatalf("bad base %q", p.Base)
					}
					got = append(got, netmodel.Prefix{Base: base, Len: p.Len})
				}
			}
			if len(got) != len(want) {
				t.Fatalf("prefix count got %d want %d", len(got), len(want))
			}
			for i := range want {
				if got[i].Len != want[i].Len || got[i].Base.Cmp(want[i].Base) != 0 {
					t.Fatalf("prefix #%d got %s/%d want %s/%d",
						i, got[i].Base, got[i].Len, want[i].Base, want[i].Len)
				}
			}
		})
	}

	// Direct interval-level extreme: a giant aligned run that does not start
	// at a prefix boundary, checked at netmodel level against the big trie.
	t.Run("huge unaligned interval", func(t *testing.T) {
		lo := new(big.Int).Lsh(one, 100)              // 2^100
		hi := new(big.Int).Sub(max128, big.NewInt(7)) // up to ...fff8
		got := netmodel.IntervalToPrefixes(128, lo, hi)
		want := refmodel.BigTrieCover(128,
			[]refmodel.BigInterval{{Lo: lo, Hi: hi}}, nil)
		if len(got) != len(want) {
			t.Fatalf("count got %d want %d", len(got), len(want))
		}
	})
}

// TestPrefixBoundaryPolicy locks the explicit network+broadcast semantics.
func TestPrefixBoundaryPolicy(t *testing.T) {
	// /30 contains exactly four addresses including .0 and .3.
	res := engine.Compute([]string{"192.168.1.0/30"}, nil, engine.Options{})
	if res.TotalCovered != "4" {
		t.Fatalf("/30 must cover 4 addresses (network+broadcast included), got %s", res.TotalCovered)
	}
	// Excluding just the broadcast leaves 3.
	res = engine.Compute([]string{"192.168.1.0/30"}, []string{"192.168.1.3/32"}, engine.Options{})
	if res.TotalCovered != "3" {
		t.Fatalf("expected 3 after excluding broadcast, got %s", res.TotalCovered)
	}
}
