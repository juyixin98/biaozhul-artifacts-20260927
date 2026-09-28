package netmodel_test

import (
	"errors"
	"math/big"
	"net/netip"
	"testing"

	"cidrsvc/internal/netmodel"
)

func requireKind(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error kind %s, got nil", want)
	}
	var pe *netmodel.PrefixError
	if !errors.As(err, &pe) {
		t.Fatalf("error %v is not PrefixError", err)
	}
	if pe.Kind != want {
		t.Fatalf("error kind = %s, want %s (err=%v)", pe.Kind, want, err)
	}
}

func TestParseErrorCategories(t *testing.T) {
	cases := []struct {
		in   string
		kind string
	}{
		{"10.0.0.0/33", netmodel.KindPrefixTooLong},
		{"2001:db8::/129", netmodel.KindPrefixTooLong},
		{"10.0.0.0", netmodel.KindMalformed},
		{"not-an-ip/8", netmodel.KindMalformed},
		{"10.0.0.0/abc", netmodel.KindMalformed},
		{"10.0.0.5/24", netmodel.KindHostBits},
		{"2001:db8::1/64", netmodel.KindHostBits},
	}
	for _, c := range cases {
		_, err := netmodel.ParsePrefix(c.in)
		requireKind(t, err, c.kind)
	}
}

func TestLenientMasksHostBitsWithWarning(t *testing.T) {
	p, warn, err := netmodel.ParsePrefixLenient("10.0.0.5/24")
	if err != nil {
		t.Fatal(err)
	}
	if p.String() != "10.0.0.0/24" {
		t.Fatalf("canonical = %s", p)
	}
	if warn == "" {
		t.Fatal("expected canonicalisation warning")
	}
}

func TestIPv4ConcreteCovers(t *testing.T) {
	cases := []struct {
		name    string
		allow   []string
		exclude []string
		want    []string
	}{
		{
			name:  "full space",
			allow: []string{"0.0.0.0/0"},
			want:  []string{"0.0.0.0/0"},
		},
		{
			name:    "exclude everything",
			allow:   []string{"0.0.0.0/0"},
			exclude: []string{"0.0.0.0/0"},
			want:    nil,
		},
		{
			name:  "single /24",
			allow: []string{"10.0.0.0/24"},
			want:  []string{"10.0.0.0/24"},
		},
		{
			name:    "carve first address out of /24",
			allow:   []string{"10.0.0.0/24"},
			exclude: []string{"10.0.0.0/32"},
			want: []string{
				"10.0.0.1/32", "10.0.0.2/31", "10.0.0.4/30",
				"10.0.0.8/29", "10.0.0.16/28", "10.0.0.32/27",
				"10.0.0.64/26", "10.0.0.128/25",
			},
		},
		{
			name:    "carve last address out of /24 (broadcast included in allow)",
			allow:   []string{"10.0.0.0/24"},
			exclude: []string{"10.0.0.255/32"},
			want: []string{
				"10.0.0.0/25", "10.0.0.128/26", "10.0.0.192/27",
				"10.0.0.224/28", "10.0.0.240/29", "10.0.0.248/30",
				"10.0.0.252/31", "10.0.0.254/32",
			},
		},
		{
			name:  "adjacent /25 siblings merge to /24",
			allow: []string{"192.168.1.0/25", "192.168.1.128/25"},
			want:  []string{"192.168.1.0/24"},
		},
		{
			name: "four /26 merge to /24",
			allow: []string{
				"172.16.0.0/26", "172.16.0.64/26", "172.16.0.128/26", "172.16.0.192/26",
			},
			want: []string{"172.16.0.0/24"},
		},
		{
			name:    "hole in middle",
			allow:   []string{"10.1.0.0/16"},
			exclude: []string{"10.1.1.0/24"},
			want: []string{
				"10.1.0.0/24",
				"10.1.2.0/23", "10.1.4.0/22", "10.1.8.0/21",
				"10.1.16.0/20", "10.1.32.0/19", "10.1.64.0/18",
				"10.1.128.0/17",
			},
		},
		{
			name:    "overlapping allows; sibling blocks must merge after carve",
			allow:   []string{"203.0.113.0/25", "203.0.113.0/24"},
			exclude: []string{"203.0.113.200/29"},
			want: []string{
				"203.0.113.0/25", "203.0.113.128/26", "203.0.113.192/29",
				"203.0.113.208/28", "203.0.113.224/27",
			},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res, err := netmodel.Compute(netmodel.ComputeRequest{
				Allow: c.allow, Exclude: c.exclude, Strict: true,
			})
			if err != nil {
				t.Fatal(err)
			}
			if len(res.Prefixes) != len(c.want) {
				t.Fatalf("got %v\nwant %v\nsteps=%v", res.Prefixes, c.want, res.Steps)
			}
			for i := range c.want {
				if res.Prefixes[i] != c.want[i] {
					t.Fatalf("index %d got %s want %s\nfull=%v", i, res.Prefixes[i], c.want[i], res.Prefixes)
				}
			}
			if res.Proof.CoverAddressCount != res.Proof.TargetAddressCount {
				t.Fatal("address counts differ")
			}
		})
	}
}

func TestBoundaryPolicyNetworkAndBroadcastInclusive(t *testing.T) {
	// A /31 keeps both addresses; a /32 carve at either end proves neither
	// boundary address is silently dropped like some host-range conventions do.
	res, err := netmodel.Compute(netmodel.ComputeRequest{
		Allow:   []string{"198.51.100.0/31"},
		Exclude: nil,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := res.Prefixes; len(got) != 1 || got[0] != "198.51.100.0/31" {
		t.Fatalf("/31 not preserved: %v", got)
	}
	if res.Proof.CoverAddressCount != "2" {
		t.Fatalf("/31 must cover 2 addresses (network+broadcast), got %s", res.Proof.CoverAddressCount)
	}
}

func TestIPv6Extremes(t *testing.T) {
	t.Run("full v6 space is one /0", func(t *testing.T) {
		res, err := netmodel.Compute(netmodel.ComputeRequest{Allow: []string{"::/0"}})
		if err != nil {
			t.Fatal(err)
		}
		if len(res.Prefixes) != 1 || res.Prefixes[0] != "::/0" {
			t.Fatalf("got %v", res.Prefixes)
		}
		if res.Proof.CoverAddressCount != new(big.Int).Lsh(big.NewInt(1), 128).String() {
			t.Fatalf("count=%s", res.Proof.CoverAddressCount)
		}
	})

	t.Run("carve :: out of full space (lowest address)", func(t *testing.T) {
		res, err := netmodel.Compute(netmodel.ComputeRequest{
			Allow:   []string{"::/0"},
			Exclude: []string{"::/128"},
		})
		if err != nil {
			t.Fatal(err)
		}
		// Independent expectation: removing ordinal 0 from a 128-bit space
		// leaves one block at every prefix length 128,127,...,1 whose network
		// ordinal is 2^(128-p). Built directly from arithmetic, not from Cover.
		var want []string
		for p := 128; p >= 1; p-- {
			net := new(big.Int).Lsh(big.NewInt(1), uint(128-p))
			want = append(want, ordinalV6(net)+"/"+itoaS(p))
		}
		assertPrefixes(t, res.Prefixes, want)
		if res.Proof.CoverAddressCount != subPow2(128, 1) {
			t.Fatalf("count=%s", res.Proof.CoverAddressCount)
		}
	})

	t.Run("carve highest address ffff...ffff out", func(t *testing.T) {
		res, err := netmodel.Compute(netmodel.ComputeRequest{
			Allow:   []string{"::/0"},
			Exclude: []string{"ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff/128"},
		})
		if err != nil {
			t.Fatal(err)
		}
		// Independent expectation: blocks of prefix lengths 1..128 ending
		// exactly at ordinal 2^128-2 (the highest address is removed). The /p
		// network is 2^128 - 2^(128-p) - 2^(128-p) = 2^128 - 2^(129-p).
		var want []string
		for p := 1; p <= 128; p++ {
			net := new(big.Int).Sub(
				new(big.Int).Lsh(big.NewInt(1), 128),
				new(big.Int).Lsh(big.NewInt(1), uint(129-p)))
			want = append(want, ordinalV6(net)+"/"+itoaS(p))
		}
		assertPrefixes(t, res.Prefixes, want)
		if res.Proof.CoverAddressCount != subPow2(128, 1) {
			t.Fatalf("count=%s", res.Proof.CoverAddressCount)
		}
	})

	t.Run("only lowest and highest addresses remain", func(t *testing.T) {
		res, err := netmodel.Compute(netmodel.ComputeRequest{
			Allow:   []string{"::/128", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff/128"},
			Exclude: nil,
		})
		if err != nil {
			t.Fatal(err)
		}
		if len(res.Prefixes) != 2 ||
			res.Prefixes[0] != "::/128" ||
			res.Prefixes[1] != "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff/128" {
			t.Fatalf("got %v", res.Prefixes)
		}
		if res.Proof.CoverAddressCount != "2" {
			t.Fatalf("count=%s", res.Proof.CoverAddressCount)
		}
	})

	t.Run("typical v6 allocation difference", func(t *testing.T) {
		res, err := netmodel.Compute(netmodel.ComputeRequest{
			Allow:   []string{"2001:db8::/32"},
			Exclude: []string{"2001:db8:1::/48"},
		})
		if err != nil {
			t.Fatal(err)
		}
		// Independent expectation built from ordinals: remainder of the /32
		// after removing the /48 at sub-position 1.
		base := new(big.Int)
		if _, ok := base.SetString("20010db8000000000000000000000000", 16); !ok {
			t.Fatal("bad base")
		}
		var want []string
		// block at position 0 stays /48; remaining positions 2..65535 then
		// greedily merge: /47 at position 2, /46 at 4, /45 at 8, ... /33 at
		// 32768. Position of a /p block is 2^(48-p), built purely arithmetically.
		net := new(big.Int).Set(base)
		want = append(want, ordinalV6(net)+"/48")
		for p := 47; p >= 33; p-- {
			position := new(big.Int).Lsh(big.NewInt(1), uint(48-p)) // 2,4,8,...
			off := new(big.Int).Lsh(position, 80)                   // *2^80
			want = append(want, ordinalV6(new(big.Int).Add(base, off))+"/"+itoaS(p))
		}
		assertPrefixes(t, res.Prefixes, want)
	})
}

func subPow2(exp, sub int64) string {
	return new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), uint(exp)), big.NewInt(sub)).String()
}

// ordinalV6 formats a 128-bit address ordinal in compressed IPv6 text.
func ordinalV6(n *big.Int) string {
	var b [16]byte
	n.FillBytes(b[:])
	return netip.AddrFrom16(b).String()
}

func itoaS(i int) string {
	if i == 0 {
		return "0"
	}
	var buf [4]byte
	pos := len(buf)
	for i > 0 {
		pos--
		buf[pos] = byte('0' + i%10)
		i /= 10
	}
	return string(buf[pos:])
}

func assertPrefixes(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("count got=%d want=%d\ngot=%v\nwant=%v", len(got), len(want), got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("index %d got=%s want=%s\nfull=%v", i, got[i], want[i], got)
		}
	}
}

func TestFamilyMismatch(t *testing.T) {
	_, err := netmodel.Compute(netmodel.ComputeRequest{
		Allow:   []string{"10.0.0.0/8"},
		Exclude: []string{"2001:db8::/32"},
	})
	requireKind(t, err, netmodel.KindFamilyMismatch)

	_, err = netmodel.Compute(netmodel.ComputeRequest{
		Family: "ipv4",
		Allow:  []string{"2001:db8::/32"},
	})
	requireKind(t, err, netmodel.KindFamilyMismatch)
}

func TestEmptyInputSets(t *testing.T) {
	res, err := netmodel.Compute(netmodel.ComputeRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if !res.EmptyResult || len(res.Prefixes) != 0 || res.Proof.TargetAddressCount != "0" {
		t.Fatalf("empty request must yield empty cover: %+v", res)
	}

	res, err = netmodel.Compute(netmodel.ComputeRequest{
		Allow:   []string{"10.0.0.0/24", "192.168.0.0/16"},
		Exclude: []string{"0.0.0.0/0"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.EmptyResult {
		t.Fatalf("excluding universe must leave nothing: %v", res.Prefixes)
	}
}

func TestStepsAndProofArePopulated(t *testing.T) {
	res, err := netmodel.Compute(netmodel.ComputeRequest{
		Allow:   []string{"10.0.0.0/24"},
		Exclude: []string{"10.0.0.128/25"},
	})
	if err != nil {
		t.Fatal(err)
	}
	wantSteps := []string{"resolve_family", "parse_allow", "parse_exclude",
		"union_and_subtract", "greedy_cover", "convert_and_format", "independent_verification"}
	if len(res.Steps) != len(wantSteps) {
		t.Fatalf("steps=%v", res.Steps)
	}
	for i, n := range wantSteps {
		if res.Steps[i].Name != n {
			t.Fatalf("step %d = %s want %s", i, res.Steps[i].Name, n)
		}
	}
	if !res.Proof.Equivalent || len(res.Proof.SiblingMerges) != 0 {
		t.Fatalf("proof=%+v", res.Proof)
	}
}
