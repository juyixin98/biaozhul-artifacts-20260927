package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDefaultIsValidRFC(t *testing.T) {
	c := Default()
	if err := c.Validate(); err != nil {
		t.Fatalf("defaults invalid: %v", err)
	}
	if c.Timing.QueryInterval != 125000 ||
		c.Timing.QueryResponseInterval != 10000 ||
		c.Timing.LastMemberQueryInterval != 1000 ||
		c.Timing.LastMemberQueryCount != 2 {
		t.Errorf("unexpected RFC defaults: %+v", c.Timing)
	}
}

func TestRejectsBadTiming(t *testing.T) {
	c := Default()
	c.Timing.QueryResponseInterval = c.Timing.QueryInterval
	if err := c.Validate(); err == nil {
		t.Error("QRI must be < QI")
	}
	c = Default()
	c.Timing.GroupMembershipInterval = 0
	if err := c.Validate(); err == nil {
		t.Error("GMI zero must fail")
	}
	c = Default()
	c.Timing.LastMemberQueryCount = 0
	if err := c.Validate(); err == nil {
		t.Error("LMQC zero must fail")
	}
	c = Default()
	c.Interfaces = nil
	if err := c.Validate(); err == nil {
		t.Error("no interfaces must fail")
	}
	c = Default()
	c.Interfaces = append(c.Interfaces, Interface{Name: "eth0"})
	if err := c.Validate(); err == nil {
		t.Error("duplicate interface must fail")
	}
}

func TestOverlayMerge(t *testing.T) {
	base := Default().Timing
	merged := base.MergeOverlay(Timing{GroupMembershipInterval: 260, LastMemberQueryCount: 3})
	if merged.GroupMembershipInterval != 260 {
		t.Error("overlay not applied")
	}
	if merged.QueryInterval != base.QueryInterval {
		t.Error("unset overlay field must preserve base")
	}
	if merged.LastMemberQueryCount != 3 {
		t.Error("LMQC overlay not applied")
	}
}

func TestLoadMissingFile(t *testing.T) {
	dir := t.TempDir()
	if _, err := Load(filepath.Join(dir, "nope.json")); err == nil {
		t.Error("missing config must error")
	}
	good := filepath.Join(dir, "c.json")
	if err := os.WriteFile(good, []byte(`{"service_name":"x","http_addr":"127.0.0.1:0"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	c, err := Load(good)
	if err != nil {
		t.Fatal(err)
	}
	if c.ServiceName != "x" {
		t.Errorf("service=%s", c.ServiceName)
	}
	if err := c.Validate(); err != nil {
		t.Errorf("partial file should fall back to defaults then validate: %v", err)
	}
}
