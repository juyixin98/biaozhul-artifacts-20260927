// Package config parses and validates the NAT laboratory configuration from
// JSON files and environment overrides.
package config

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strconv"
	"time"
)

// Config is the complete boundary configuration for a replay run.
type Config struct {
	// ExternalIP is the simulated NAT public address (synthetic; never bound).
	ExternalIP string `json:"external_ip"`

	// Port ranges per protocol. An external port is allocated inside the range
	// and is never reused by a second active mapping of the same protocol.
	TCPPortMin uint16 `json:"tcp_port_min"`
	TCPPortMax uint16 `json:"tcp_port_max"`
	UDPPortMin uint16 `json:"udp_port_min"`
	UDPPortMax uint16 `json:"udp_port_max"`

	// Separate TCP/UDP timeouts (spec requirement).
	TCPSynTimeout   Duration `json:"tcp_syn_timeout"` // SYN_SENT idle timeout
	TCPEstabTimeout Duration `json:"tcp_established_timeout"`
	TCPFinTimeout   Duration `json:"tcp_fin_timeout"` // FIN_WAIT / half-closed
	TCPTimeWait     Duration `json:"tcp_time_wait_timeout"`
	UDPTimeout      Duration `json:"udp_timeout"`

	// ReuseCooldown forbids reallocation of a freed external port until this
	// long after release (0 = reuse immediately). It does NOT keep old mappings
	// alive.
	ReuseCooldown Duration `json:"reuse_cooldown"`

	// Persistence / replay interface.
	SQLitePath string `json:"sqlite_path"` // "" = in-memory store
	HTTPListen string `json:"http_listen"`

	// ReaperGap is the granularity at which the replay clock lazily expires
	// mappings; it only affects timing of reaping, never semantics.
	ReaperGap Duration `json:"reaper_gap"`
}

// Duration wraps time.Duration with JSON (un)marshalling in human form
// ("30s", "2m").
type Duration struct{ time.Duration }

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.String())
}

func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return fmt.Errorf("invalid duration %q: %w", s, err)
	}
	d.Duration = v
	return nil
}

// Default returns the synthetic-lab defaults. Every address is documentation;
// nothing is ever bound to the system network by the core.
func Default() Config {
	return Config{
		ExternalIP:      "198.51.100.1", // TEST-NET-2, reserved for examples
		TCPPortMin:      20000,
		TCPPortMax:      20099,
		UDPPortMin:      30000,
		UDPPortMax:      30099,
		TCPSynTimeout:   Duration{30 * time.Second},
		TCPEstabTimeout: Duration{5 * time.Minute},
		TCPFinTimeout:   Duration{2 * time.Minute},
		TCPTimeWait:     Duration{30 * time.Second},
		UDPTimeout:      Duration{2 * time.Minute},
		ReuseCooldown:   Duration{0},
		SQLitePath:      "",
		HTTPListen:      "127.0.0.1:18080",
		ReaperGap:       Duration{time.Second},
	}
}

// Load reads a JSON config file. An empty path returns Default().
func Load(path string) (Config, error) {
	cfg := Default()
	if path == "" {
		return cfg, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config %q: %w", path, err)
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, fmt.Errorf("parse config %q: %w", path, err)
	}
	applyEnv(&cfg)
	if err := cfg.Validate(); err != nil {
		return cfg, fmt.Errorf("validate config %q: %w", path, err)
	}
	return cfg, nil
}

// NATLAB_* overrides exist for the fields a CI harness commonly changes.
func applyEnv(cfg *Config) {
	if v := os.Getenv("NATLAB_SQLITE_PATH"); v != "" {
		cfg.SQLitePath = v
	}
	if v := os.Getenv("NATLAB_HTTP_LISTEN"); v != "" {
		cfg.HTTPListen = v
	}
	if v := os.Getenv("NATLAB_EXTERNAL_IP"); v != "" {
		cfg.ExternalIP = v
	}
}

// Validate enforces the data contract before the engine is constructed.
func (c Config) Validate() error {
	if ip := net.ParseIP(c.ExternalIP); ip == nil {
		return fmt.Errorf("external_ip %q is not a valid IP", c.ExternalIP)
	}
	check := func(name string, lo, hi uint16) error {
		if lo == 0 || hi == 0 {
			return fmt.Errorf("%s: port range must be non-zero", name)
		}
		if lo > hi {
			return fmt.Errorf("%s: min %d > max %d", name, lo, hi)
		}
		if lo < 1024 {
			return fmt.Errorf("%s: ephemeral range must be >= 1024, got %d", name, lo)
		}
		return nil
	}
	if err := check("tcp", c.TCPPortMin, c.TCPPortMax); err != nil {
		return err
	}
	if err := check("udp", c.UDPPortMin, c.UDPPortMax); err != nil {
		return err
	}
	tos := []struct {
		name string
		d    time.Duration
	}{
		{"tcp_syn_timeout", c.TCPSynTimeout.Duration},
		{"tcp_established_timeout", c.TCPEstabTimeout.Duration},
		{"tcp_fin_timeout", c.TCPFinTimeout.Duration},
		{"tcp_time_wait_timeout", c.TCPTimeWait.Duration},
		{"udp_timeout", c.UDPTimeout.Duration},
	}
	for _, t := range tos {
		if t.d <= 0 {
			return fmt.Errorf("%s must be > 0", t.name)
		}
	}
	if c.ReuseCooldown.Duration < 0 {
		return fmt.Errorf("reuse_cooldown must be >= 0")
	}
	if c.HTTPListen != "" {
		if _, _, err := net.SplitHostPort(c.HTTPListen); err != nil {
			return fmt.Errorf("http_listen %q: %w", c.HTTPListen, err)
		}
	}
	return nil
}

// MustPortCount is a test/helper for sizing expected exhaustion.
func PortCount(lo, hi uint16) int { return int(hi) - int(lo) + 1 }

// EnvInt is a small helper used by example tooling.
func EnvInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
