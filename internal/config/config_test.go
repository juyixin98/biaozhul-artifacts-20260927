package config_test

import (
	"net/netip"
	"os"
	"path/filepath"
	"testing"

	"natlab/internal/config"
	"natlab/internal/model"
)

func TestDefaultsAreValid(t *testing.T) {
	cfg := config.Defaults()
	if err := cfg.Resolve(); err != nil {
		t.Fatalf("defaults invalid: %v", err)
	}
	if !cfg.PublicAddr().Is4() || cfg.PublicIPString() != "203.0.113.1" {
		t.Fatalf("public addr=%s", cfg.PublicIPString())
	}
	if !cfg.IsPrivate(mustAddr(t, "10.1.2.3")) {
		t.Fatal("10/8 should be private")
	}
	if cfg.IsPrivate(mustAddr(t, "203.0.113.10")) {
		t.Fatal("public address must not be private")
	}
	// TCP and UDP timeouts are independently configured.
	tcp, ok1 := cfg.TTL(model.TCP, model.StateEstablished)
	udp, ok2 := cfg.TTL(model.UDP, model.StateOpen)
	if !ok1 || !ok2 || tcp == udp {
		t.Fatalf("tcp/udp TTL not separate: tcp=%v udp=%v", tcp, udp)
	}
	if _, ok := cfg.TTL(model.UDP, model.StateEstablished); ok {
		t.Fatal("UDP has no established state; TTL lookup must fail")
	}
}

func TestLoadFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "c.json")
	body := `{
	  "listen_addr":"127.0.0.1:9999",
	  "public_ip":"198.51.100.7",
	  "private_cidrs":["10.0.0.0/8"],
	  "port_pool_low":50000,
	  "port_pool_high":50010,
	  "timeouts":{"tcp_syn_sent":"2s","tcp_transient":"4s","tcp_established":"8s","udp":"3s"}
	}`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ListenAddr != "127.0.0.1:9999" || cfg.PortLow != 50000 || cfg.PortHigh != 50010 {
		t.Fatalf("fields not loaded: %+v", cfg)
	}
	if udp, _ := cfg.TTL(model.UDP, model.StateOpen); udp.String() != "3s" {
		t.Fatalf("udp ttl=%v", udp)
	}
}

func TestInvalidConfigs(t *testing.T) {
	cases := map[string]string{
		"bad public ip":      `{"public_ip":"not-an-ip","timeouts":{"tcp_syn_sent":"1s","tcp_transient":"1s","tcp_established":"1s","udp":"1s"}}`,
		"public is ipv6":     `{"public_ip":"::1","timeouts":{"tcp_syn_sent":"1s","tcp_transient":"1s","tcp_established":"1s","udp":"1s"}}`,
		"bad cidr":           `{"public_ip":"203.0.113.1","private_cidrs":["nope"],"timeouts":{"tcp_syn_sent":"1s","tcp_transient":"1s","tcp_established":"1s","udp":"1s"}}`,
		"empty private":      `{"public_ip":"203.0.113.1","private_cidrs":[],"timeouts":{"tcp_syn_sent":"1s","tcp_transient":"1s","tcp_established":"1s","udp":"1s"}}`,
		"port range":         `{"public_ip":"203.0.113.1","port_pool_low":500,"port_pool_high":100,"timeouts":{"tcp_syn_sent":"1s","tcp_transient":"1s","tcp_established":"1s","udp":"1s"}}`,
		"negative duration":  `{"public_ip":"203.0.113.1","timeouts":{"tcp_syn_sent":"-1s","tcp_transient":"1s","tcp_established":"1s","udp":"1s"}}`,
		"malformed duration": `{"public_ip":"203.0.113.1","timeouts":{"tcp_syn_sent":"soon","tcp_transient":"1s","tcp_established":"1s","udp":"1s"}}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "c.json")
			if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
				t.Fatal(err)
			}
			if _, err := config.Load(path); err == nil {
				t.Fatalf("%s: expected validation error", name)
			}
		})
	}
}

func mustAddr(t *testing.T, s string) netip.Addr {
	t.Helper()
	a, err := netip.ParseAddr(s)
	if err != nil {
		t.Fatal(err)
	}
	return a
}
