package model

import "testing"

func TestParsePrefixCanonical(t *testing.T) {
	if got, err := ParsePrefix("10.1.0.0/16"); err != nil || got != "10.1.0.0/16" {
		t.Fatalf("canonical prefix: got %q err=%v", got, err)
	}
	if _, err := ParsePrefix("10.1.2.3/16"); err == nil {
		t.Fatal("non-masked prefix must be rejected")
	}
	if _, err := ParsePrefix("not-a-prefix"); err == nil {
		t.Fatal("garbage prefix must be rejected")
	}
	if _, err := ParsePrefix("10.0.0.0/8"); err != nil {
		t.Fatalf("/8 rejected: %v", err)
	}
}

func TestTopologyValidation(t *testing.T) {
	base := func() Topology {
		return Topology{Nodes: []Node{
			{RouterID: "a", ASN: 65001, Address: "10.0.0.1", IGPCost: 1},
			{RouterID: "b", ASN: 65002, Address: "10.0.0.2", IGPCost: 2},
		}, Links: []Link{{Src: "a", Dst: "b"}}}
	}

	t.Run("valid", func(t *testing.T) {
		idx, err := base().Build()
		if err != nil {
			t.Fatal(err)
		}
		if ns := idx.Neighbors("a"); len(ns) != 1 || ns[0] != "b" {
			t.Fatalf("neighbors a = %v", ns)
		}
		if idx.SameAS("a", "b") {
			t.Fatal("a/b must be different AS")
		}
	})

	cases := []struct {
		name   string
		mutate func(*Topology)
	}{
		{"empty nodes", func(tp *Topology) { tp.Nodes = nil }},
		{"empty router id", func(tp *Topology) { tp.Nodes[0].RouterID = "" }},
		{"duplicate router id", func(tp *Topology) { tp.Nodes[1].RouterID = "a" }},
		{"bad asn zero", func(tp *Topology) { tp.Nodes[0].ASN = 0 }},
		{"negative igp cost", func(tp *Topology) { tp.Nodes[0].IGPCost = -1 }},
		{"bad address", func(tp *Topology) { tp.Nodes[0].Address = "9.9.9" }},
		{"duplicate address", func(tp *Topology) { tp.Nodes[1].Address = "10.0.0.1" }},
		{"dangling link src", func(tp *Topology) { tp.Links[0].Src = "x" }},
		{"dangling link dst", func(tp *Topology) { tp.Links[0].Dst = "x" }},
		{"self link", func(tp *Topology) { tp.Links[0].Dst = "a" }},
		{"duplicate session", func(tp *Topology) {
			tp.Links = append(tp.Links, Link{Src: "b", Dst: "a"})
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tp := base()
			tc.mutate(&tp)
			if _, err := tp.Build(); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestParseOrigin(t *testing.T) {
	for s, want := range map[string]Origin{"": OriginIGP, "igp": OriginIGP, "EGP": OriginEGP, "incomplete": OriginIncomplete} {
		got, err := ParseOrigin(s)
		if err != nil || got != want {
			t.Fatalf("ParseOrigin(%q)=%v err=%v want %v", s, got, err, want)
		}
	}
	if _, err := ParseOrigin("bogus"); err == nil {
		t.Fatal("bogus origin accepted")
	}
}
