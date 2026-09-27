package config

import (
	"strings"
	"testing"

	"pathvector/internal/ierr"
	"pathvector/internal/model"
)

func u32(v uint32) *uint32 { return &v }

func validCfg() *Config {
	return &Config{
		Topology: model.Topology{Nodes: []model.Node{
			{RouterID: "a", ASN: 65001, Address: "10.0.0.1", IGPCost: 1},
			{RouterID: "b", ASN: 65002, Address: "10.0.0.2", IGPCost: 2},
			{RouterID: "c", ASN: 65001, Address: "10.0.0.3", IGPCost: 3},
		}, Links: []model.Link{{Src: "a", Dst: "b"}, {Src: "a", Dst: "c"}}},
		Events: []model.SeedEvent{
			{Seq: 1, Kind: model.SeedAnnounce, RouterID: "a", Prefix: "10.1.0.0/16", NextHop: "10.0.0.1"},
		},
		Budget:   50,
		QueueCap: 100,
	}
}

func TestValidateDefaults(t *testing.T) {
	c := validCfg()
	c.Budget, c.QueueCap = 0, 0
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	if c.Budget != defaultBudget || c.QueueCap != defaultQueueCap {
		t.Fatalf("defaults: budget=%d cap=%d", c.Budget, c.QueueCap)
	}
}

func TestValidateErrors(t *testing.T) {
	cases := []struct {
		name    string
		mutate  func(*Config)
		wantSub string
	}{
		{"negative budget", func(c *Config) { c.Budget = -1 }, "budget"},
		{"unknown policy router", func(c *Config) {
			c.Policies = map[string]*Policy{"x": {}}
		}, "unknown router"},
		{"unnamed rule", func(c *Config) {
			c.Policies = map[string]*Policy{"a": {Import: []Rule{{Action: Action{Allow: true}}}}}
		}, "no name"},
		{"duplicate rule name", func(c *Config) {
			c.Policies = map[string]*Policy{"a": {Import: []Rule{
				{Name: "r1", Action: Action{Allow: true}},
				{Name: "r1", Action: Action{Allow: true}},
			}}}
		}, "duplicate rule"},
		{"deny with rewrite", func(c *Config) {
			c.Policies = map[string]*Policy{"a": {Import: []Rule{
				{Name: "r1", Action: Action{Allow: false, SetLocalPref: u32(1)}},
			}}}
		}, "deny action cannot"},
		{"prepend on import", func(c *Config) {
			c.Policies = map[string]*Policy{"a": {Import: []Rule{
				{Name: "r1", Action: Action{Allow: true, Prepend: 1}},
			}}}
		}, "export-only"},
		{"set_med on import", func(c *Config) {
			c.Policies = map[string]*Policy{"a": {Import: []Rule{
				{Name: "r1", Action: Action{Allow: true, SetMED: u32(1)}},
			}}}
		}, "export-only"},
		{"set_local_pref on export", func(c *Config) {
			c.Policies = map[string]*Policy{"a": {Export: []Rule{
				{Name: "r1", Action: Action{Allow: true, SetLocalPref: u32(1)}},
			}}}
		}, "import-only"},
		{"match unknown peer", func(c *Config) {
			c.Policies = map[string]*Policy{"a": {Import: []Rule{
				{Name: "r1", Match: Match{Peer: "x"}, Action: Action{Allow: true}},
			}}}
		}, "not in topology"},
		{"bad prepend range", func(c *Config) {
			c.Policies = map[string]*Policy{"a": {Export: []Rule{
				{Name: "r1", Action: Action{Allow: true, Prepend: 99}},
			}}}
		}, "out of range"},
		{"duplicate event seq", func(c *Config) {
			c.Events = append(c.Events, model.SeedEvent{Seq: 1, Kind: model.SeedAnnounce, RouterID: "a", Prefix: "10.1.0.0/16", NextHop: "10.0.0.1"})
		}, "duplicate seq"},
		{"zero seq", func(c *Config) { c.Events[0].Seq = 0 }, "seq must be > 0"},
		{"unknown event router", func(c *Config) { c.Events[0].RouterID = "x" }, "unknown router"},
		{"bad event prefix", func(c *Config) { c.Events[0].Prefix = "10.1/16" }, "bad prefix"},
		{"event non-peer", func(c *Config) { c.Events[0].Peer = "b"; c.Events[0].RouterID = "c" }, "no session"},
		{"local announce without next_hop", func(c *Config) { c.Events[0].NextHop = "" }, "next_hop"},
		{"bad kind", func(c *Config) { c.Events[0].Kind = "boom" }, "unknown kind"},
		{"bad origin", func(c *Config) { c.Events[0].Origin = "weird" }, "origin"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := validCfg()
			tc.mutate(c)
			err := c.Validate()
			if err == nil {
				t.Fatal("expected invalid_input error")
			}
			if !ierr.Is(err, ierr.KindInvalidInput) {
				t.Fatalf("kind=%s want invalid_input", ierr.Of(err))
			}
			if !strings.Contains(err.Error(), tc.wantSub) {
				t.Fatalf("error %q does not contain %q", err.Error(), tc.wantSub)
			}
		})
	}
}

func TestParseRejectsUnknownFieldsAndTrailing(t *testing.T) {
	if _, err := Parse([]byte(`{"topology":{"nodes":[],"links":[]},"bogus":1}`)); err == nil {
		t.Fatal("unknown field accepted")
	}
	if _, err := Parse([]byte(`{"topology":{"nodes":[],"links":[]}} {}`)); err == nil {
		t.Fatal("trailing JSON accepted")
	}
	if _, err := Parse([]byte(`{not json`)); err == nil {
		t.Fatal("invalid JSON accepted")
	}
}

func TestPolicyApplyChains(t *testing.T) {
	p := &Policy{
		Import: []Rule{
			{Name: "deny-b", Match: Match{Peer: "b"}, Action: Action{Allow: false}},
			{Name: "boost", Match: Match{}, Action: Action{Allow: true, SetLocalPref: u32(500)}},
		},
		Export: []Rule{
			{Name: "prepend-to-c", Match: Match{Peer: "c"}, Action: Action{Allow: true, Prepend: 2}},
		},
	}
	base := model.Candidate{Prefix: "10.1.0.0/16", FromPeer: "b", Attrs: model.Attrs{LocalPref: 100}}

	// First-match deny wins.
	if _, d, ok := ApplyImport(p, "b", base); ok || d.Rule != "deny-b" {
		t.Fatalf("import deny: ok=%v rule=%s", ok, d.Rule)
	}
	// A different peer matches the permit/rewrite rule.
	got, d, ok := ApplyImport(p, "z", base)
	if !ok || d.Rule != "boost" || got.Attrs.LocalPref != 500 {
		t.Fatalf("import permit: ok=%v rule=%s lp=%d", ok, d.Rule, got.Attrs.LocalPref)
	}
	// Export prepend count surfaces without mutating AS_PATH here.
	out, act, ok := ApplyExport(p, "c", base)
	if !ok || act.PrependCount != 2 || len(out.Attrs.ASPath) != 0 {
		t.Fatalf("export: ok=%v prepend=%d pathlen=%d", ok, act.PrependCount, len(out.Attrs.ASPath))
	}
	// Missing policy = default permit.
	if _, d, ok := ApplyImport(nil, "b", base); !ok || d.Rule != "default-permit" {
		t.Fatal("nil policy must default-permit")
	}
}
