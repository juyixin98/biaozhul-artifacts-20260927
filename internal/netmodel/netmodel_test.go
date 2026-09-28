package netmodel

import (
	"encoding/json"
	"errors"
	"net/netip"
	"testing"
)

func addr(t *testing.T, s string) netip.Addr {
	t.Helper()
	a, err := netip.ParseAddr(s)
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func ptr(a netip.Addr) *netip.Addr { return &a }

func TestParsePrefixCanonicalization(t *testing.T) {
	cases := []struct {
		in   string
		want string
		fam  Family
		bits int
	}{
		{"10.10.10.10/8", "10.0.0.0/8", AFIPv4, 8},
		{"192.168.255.255/16", "192.168.0.0/16", AFIPv4, 16},
		{"0.0.0.0/0", "0.0.0.0/0", AFIPv4, 0},
		{"255.255.255.255/32", "255.255.255.255/32", AFIPv4, 32},
		{"2001:0db8:0000:0000:0000:0000:0000:0001/32", "2001:db8::/32", AFIPv6, 32},
		{"2001:DB8::ABCD/64", "2001:db8::/64", AFIPv6, 64}, // 主机位清零 + 小写压缩
		{"::/0", "::/0", AFIPv6, 0},
		{"::1/128", "::1/128", AFIPv6, 128},
		// 规范前缀中的主机位必须被掩码清零：
		{"2001:db8:abcd:1::1/48", "2001:db8:abcd::/48", AFIPv6, 48},
	}
	for _, c := range cases {
		p, err := ParsePrefix(c.in)
		if err != nil {
			t.Fatalf("ParsePrefix(%q): %v", c.in, err)
		}
		if p.String() != c.want {
			t.Errorf("canonical(%q) = %q, want %q", c.in, p.String(), c.want)
		}
		if p.Family() != c.fam {
			t.Errorf("family(%q) = %s, want %s", c.in, p.Family(), c.fam)
		}
		if p.Bits() != c.bits {
			t.Errorf("bits(%q) = %d, want %d", c.in, p.Bits(), c.bits)
		}
	}
}

func TestParsePrefixErrors(t *testing.T) {
	for _, in := range []string{
		"", "10.0.0.0", "10.0.0.0/33", "::/129", "999.0.0.0/8",
		"2001::db8::1/64", "foo/8", "10.0.0.0/-1",
	} {
		if _, err := ParsePrefix(in); !errors.Is(err, ErrInvalidPrefix) {
			t.Errorf("ParsePrefix(%q) err=%v, want ErrInvalidPrefix", in, err)
		}
	}
}

func TestContainsAndAFIsolation(t *testing.T) {
	v4 := MustPrefix("10.0.0.0/8")
	v6 := MustPrefix("2001:db8::/32")
	if !v4.Contains(addr(t, "10.255.255.255")) {
		t.Error("v4 contains failed")
	}
	if v4.Contains(addr(t, "11.0.0.1")) {
		t.Error("v4 contains should be false outside")
	}
	// 跨族 Contains 必须为 false，即使底层 4-in-6 映射。
	if v4.Contains(addr(t, "::ffff:10.0.0.1")) {
		t.Error("AF leak: v4 prefix matched 4-in-6 address")
	}
	if !v6.Contains(addr(t, "2001:db8:dead:beef::1")) {
		t.Error("v6 contains failed")
	}
	if v6.Contains(addr(t, "2001:db9::1")) {
		t.Error("v6 contains should be false")
	}
	if FamilyOfAddr(addr(t, "::ffff:1.2.3.4")) != AFIPv4 {
		t.Error("4-in-6 must be classified as IPv4")
	}
}

func TestPrefixJSONRoundTrip(t *testing.T) {
	// 非规范输入经 JSON 反序列化后必须规范化。
	raw := []byte(`{"prefix":"2001:0DB8:0000::abcd/48"}`)
	var v struct {
		Prefix Prefix `json:"prefix"`
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if v.Prefix.String() != "2001:db8::/48" {
		t.Fatalf("got %q", v.Prefix.String())
	}
	out, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != `{"prefix":"2001:db8::/48"}` {
		t.Fatalf("marshal = %s", out)
	}

	// 非法 CIDR 必须在 JSON 层失败。
	if err := json.Unmarshal([]byte(`{"prefix":"not-a-cidr"}`), &v); !errors.Is(err, ErrInvalidPrefix) {
		t.Fatalf("err=%v want ErrInvalidPrefix", err)
	}
}

func TestRouteValidation(t *testing.T) {
	base := func() Route {
		return Route{
			ID:            "r1",
			Prefix:        MustPrefix("10.0.0.0/8"),
			AdminDistance: 5,
			Metric:        10,
			Protocol:      "static",
			Nexthop:       Nexthop{Kind: NHAddress, Address: ptr(addr(t, "10.0.0.1"))},
		}
	}

	t.Run("valid", func(t *testing.T) {
		if err := base().Validate(); err != nil {
			t.Fatal(err)
		}
	})
	t.Run("empty id", func(t *testing.T) {
		r := base()
		r.ID = ""
		if !errors.Is(r.Validate(), ErrEmptyID) {
			t.Fatal("want ErrEmptyID")
		}
	})
	t.Run("address nh requires address", func(t *testing.T) {
		r := base()
		r.Nexthop.Address = nil
		if !errors.Is(r.Validate(), ErrNHAddrRequired) {
			t.Fatal("want ErrNHAddrRequired")
		}
	})
	t.Run("connected requires iface", func(t *testing.T) {
		r := base()
		r.Nexthop = Nexthop{Kind: NHConnected}
		if !errors.Is(r.Validate(), ErrNHIfaceRequired) {
			t.Fatal("want ErrNHIfaceRequired")
		}
	})
	t.Run("blackhole rejects address", func(t *testing.T) {
		r := base()
		r.Nexthop = Nexthop{Kind: NHBlackhole, Address: ptr(addr(t, "10.0.0.9"))}
		if !errors.Is(r.Validate(), ErrNHAddrNotAllowed) {
			t.Fatal("want ErrNHAddrNotAllowed")
		}
	})
	t.Run("af mismatch", func(t *testing.T) {
		r := base()
		r.Nexthop.Address = ptr(addr(t, "2001:db8::1"))
		if !errors.Is(r.Validate(), ErrAFMismatch) {
			t.Fatal("want ErrAFMismatch")
		}
	})
	t.Run("v6 route v4 nh", func(t *testing.T) {
		r := base()
		r.Prefix = MustPrefix("2001:db8::/32")
		r.Nexthop.Address = ptr(addr(t, "10.0.0.1"))
		if !errors.Is(r.Validate(), ErrAFMismatch) {
			t.Fatal("want cross-family rejection")
		}
	})
	t.Run("bad kind/distance/metric", func(t *testing.T) {
		r := base()
		r.Nexthop.Kind = "bogus"
		if !errors.Is(r.Validate(), ErrBadNexthopKind) {
			t.Fatal("want ErrBadNexthopKind")
		}
		r = base()
		r.AdminDistance = 256
		if !errors.Is(r.Validate(), ErrBadDistance) {
			t.Fatal("want ErrBadDistance")
		}
		r = base()
		r.Metric = -1
		if !errors.Is(r.Validate(), ErrBadMetric) {
			t.Fatal("want ErrBadMetric")
		}
	})
	t.Run("blackhole json omits address", func(t *testing.T) {
		r := base()
		r.Nexthop = Nexthop{Kind: NHBlackhole}
		b, err := json.Marshal(r)
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]any
		if err := json.Unmarshal(b, &m); err != nil {
			t.Fatal(err)
		}
		nh := m["nexthop"].(map[string]any)
		if _, present := nh["address"]; present {
			t.Fatalf("address should be omitted: %s", b)
		}
	})
}
