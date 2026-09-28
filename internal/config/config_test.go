package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDefaultIsLoopbackAndValid(t *testing.T) {
	c := Default()
	if err := c.Validate(); err != nil {
		t.Fatalf("default config invalid: %v", err)
	}
	if !strings.HasPrefix(c.Server.UDPListen, "127.") || !strings.HasPrefix(c.Server.HTTPListen, "127.") {
		t.Fatalf("default listeners not loopback: %s %s", c.Server.UDPListen, c.Server.HTTPListen)
	}
	if !strings.HasPrefix(c.Pool.RangeStart, "127.") || !strings.HasPrefix(c.Pool.RangeEnd, "127.") {
		t.Fatalf("default pool not loopback: %s %s", c.Pool.RangeStart, c.Pool.RangeEnd)
	}
	if c.LeaseSeconds() != 600 {
		t.Fatalf("default lease seconds=%d want 600", c.LeaseSeconds())
	}
}

func TestRejectsNonLoopbackListener(t *testing.T) {
	c := Default()
	c.Server.UDPListen = "0.0.0.0:67"
	err := c.Validate()
	if err == nil || !strings.Contains(err.Error(), "loopback") {
		t.Fatalf("expected loopback guard error, got %v", err)
	}
}

func TestRejectsNonLoopbackPool(t *testing.T) {
	c := Default()
	c.Pool.RangeStart = "192.168.10.2"
	c.Pool.RangeEnd = "192.168.10.99"
	err := c.Validate()
	if err == nil || !strings.Contains(err.Error(), "127.0.0.0/8") {
		t.Fatalf("expected pool loopback error, got %v", err)
	}
}

func TestAllowsNonLoopbackOnlyWithOverride(t *testing.T) {
	c := Default()
	c.Server.AllowNonLoopback = true
	c.Server.UDPListen = "0.0.0.0:67"
	c.Server.HTTPListen = "0.0.0.0:8080"
	c.Pool.RangeStart = "192.168.10.2"
	c.Pool.RangeEnd = "192.168.10.99"
	c.Pool.ServerID = "192.168.10.1"
	c.Pool.Netmask = "255.255.255.0"
	c.Pool.Router = "192.168.10.1"
	if err := c.Validate(); err != nil {
		t.Fatalf("override config should validate: %v", err)
	}
}

func TestLeaseTimerOrdering(t *testing.T) {
	c := Default()
	c.Lease.T2 = Duration{12 * time.Minute}
	if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "t2") {
		t.Fatalf("expected t2 < leaseTime error, got %v", err)
	}
	c = Default()
	c.Lease.T1 = Duration{9 * time.Minute}
	c.Lease.T2 = Duration{9 * time.Minute}
	if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "t2") {
		t.Fatalf("expected t2 > t1 error, got %v", err)
	}
}

func TestInvertedRange(t *testing.T) {
	c := Default()
	c.Pool.RangeStart = "127.10.0.50"
	c.Pool.RangeEnd = "127.10.0.10"
	if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "must not exceed") {
		t.Fatalf("expected inverted range error, got %v", err)
	}
}

func TestDurationJSON(t *testing.T) {
	var d Duration
	if err := d.UnmarshalJSON([]byte(`"15s"`)); err != nil {
		t.Fatal(err)
	}
	if d.Duration != 15*time.Second {
		t.Fatalf("duration=%v", d.Duration)
	}
	if err := d.UnmarshalJSON([]byte(`"-2s"`)); err == nil {
		t.Fatal("negative duration must be rejected")
	}
	if err := d.UnmarshalJSON([]byte(`"not-a-duration"`)); err == nil {
		t.Fatal("garbage duration must be rejected")
	}
}

func TestLoadFileAndEnvOverride(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "c.json")
	if err := os.WriteFile(path, []byte(`{
		"server": {"udpListen":"127.0.0.1:12345","httpListen":"127.0.0.1:12346"},
		"pool": {"rangeStart":"127.40.0.2","rangeEnd":"127.40.0.9",
		         "serverId":"127.0.0.1","netmask":"255.0.0.0"},
		"lease": {"leaseTime":"10s","t1":"4s","t2":"8s","offerTTL":"2s"},
		"store": {"dsn":"file:test.db"}
	}`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DHCPV4LAB_UDP_LISTEN", "127.0.0.1:22222")
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if cfg.Server.UDPListen != "127.0.0.1:22222" {
		t.Fatalf("env override not applied: %s", cfg.Server.UDPListen)
	}
	if cfg.Server.HTTPListen != "127.0.0.1:12346" {
		t.Fatalf("file value not loaded: %s", cfg.Server.HTTPListen)
	}
}

func TestLoadRejectsBadJSON(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(path, []byte(`{not json`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("bad JSON must fail")
	}
}
