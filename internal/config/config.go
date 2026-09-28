// Package config defines the file format and validation rules for the local
// DHCPv4 lab server.
//
// The configuration is intentionally small and fully local: every address
// must live on loopback and every lease pool range must be loopback space
// (127.0.0.0/8) unless AllowNonLoopback is explicitly set. The default
// configuration never binds to a production interface and never allocates
// externally routable addresses.
package config

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"strings"
	"time"
)

// Duration wraps time.Duration so JSON files may express lease times as
// human-readable strings ("10s") instead of opaque nanosecond numbers.
type Duration struct{ time.Duration }

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.Duration.String())
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
	if v <= 0 {
		return fmt.Errorf("duration must be positive, got %q", s)
	}
	d.Duration = v
	return nil
}

// Config is the root configuration document.
type Config struct {
	Server ServerConfig `json:"server"`
	Pool   PoolConfig   `json:"pool"`
	Lease  LeaseConfig  `json:"lease"`
	// Options holds the options the server advertises in OFFER/ACK.
	Options OptionConfig `json:"options"`
	Store   StoreConfig  `json:"store"`
	// TestMode unlocks the test-only HTTP endpoints (clock advance, state
	// reset). It must never be enabled for an open deployment.
	TestMode bool `json:"testMode"`
}

// ServerConfig covers the two local listeners. Both default to loopback.
type ServerConfig struct {
	// UDPListen is the host:port the DHCP datagram socket binds to.
	UDPListen string `json:"udpListen"`
	// HTTPListen is the host:port the replay/admin API binds to.
	HTTPListen string `json:"httpListen"`
	// AllowNonLoopback disables the loopback-only guard. Keep false.
	AllowNonLoopback bool `json:"allowNonLoopback"`
	// ReadTimeout bounds a single UDP read cycle's housekeeping, not packets.
	ReadTimeout Duration `json:"readTimeout"`
}

// PoolConfig is the pool from which addresses are atomically allocated.
type PoolConfig struct {
	// RangeStart/RangeEnd are inclusive pool boundaries.
	RangeStart string `json:"rangeStart"`
	RangeEnd   string `json:"rangeEnd"`
	// ServerID is option 54, the server identifier in OFFER/ACK/NAK.
	ServerID string `json:"serverId"`
	// Netmask is option 1 (IPv4 dotted-quad).
	Netmask string `json:"netmask"`
	// Router is option 3. Optional.
	Router string `json:"router,omitempty"`
	// DNS is option 6. Optional.
	DNS []string `json:"dns,omitempty"`
}

// LeaseConfig configures lease timing.
type LeaseConfig struct {
	// LeaseTime is option 51 advertised in OFFER/ACK (seconds).
	LeaseTime Duration `json:"leaseTime"`
	// T1/T2 drive renewal/rebinding timers on clients. The server itself
	// treats renew/rebind identically (lease extension), matching RFC 2131.
	T1 Duration `json:"t1"`
	T2 Duration `json:"t2"`
	// OfferTTL bounds how long an OFFER reservation stays held.
	OfferTTL Duration `json:"offerTTL"`
	// SweepInterval controls the background expiry pass. Zero (default)
	// derives it from OfferTTL. Negative disables the background sweeper.
	SweepInterval *Duration `json:"sweepInterval,omitempty"`
}

// OptionConfig is the resolved option set sent to clients.
type OptionConfig struct {
	Netmask string   `json:"netmask"`
	Router  string   `json:"router,omitempty"`
	DNS     []string `json:"dns,omitempty"`
}

// StoreConfig configures persistence.
type StoreConfig struct {
	// DSN is a SQLite DSN, e.g. "file:dhcp.db" or "file::memory:?cache=shared".
	DSN string `json:"dsn"`
	// Reset wipes prior state on startup (fresh test pools).
	Reset bool `json:"reset"`
}

// Default returns the safe, fully loopback default configuration.
func Default() Config {
	return Config{
		Server: ServerConfig{
			UDPListen:   "127.0.0.1:10067",
			HTTPListen:  "127.0.0.1:18080",
			ReadTimeout: Duration{5 * time.Second},
		},
		Pool: PoolConfig{
			RangeStart: "127.10.0.2",
			RangeEnd:   "127.10.0.62",
			ServerID:   "127.0.0.1",
			Netmask:    "255.0.0.0",
			Router:     "127.0.0.1",
			DNS:        []string{"127.0.0.1"},
		},
		Lease: LeaseConfig{
			LeaseTime: Duration{10 * time.Minute},
			T1:        Duration{5 * time.Minute},
			T2:        Duration{8 * time.Minute},
			OfferTTL:  Duration{30 * time.Second},
		},
		Options: OptionConfig{
			Netmask: "255.0.0.0",
			Router:  "127.0.0.1",
			DNS:     []string{"127.0.0.1"},
		},
		Store: StoreConfig{
			DSN: "file:dhcpv4lab.db",
		},
	}
}

// Load reads, parses, applies environment overrides to and validates a config.
// The environment prefix DHCPV4LAB_ maps onto top-level scalar settings:
//
//	DHCPV4LAB_UDP_LISTEN, DHCPV4LAB_HTTP_LISTEN,
//	DHCPV4LAB_DSN, DHCPV4LAB_TEST_MODE, DHCPV4LAB_ALLOW_NON_LOOPBACK
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		raw, err := os.ReadFile(path)
		if err != nil {
			return cfg, fmt.Errorf("read config %s: %w", path, err)
		}
		if err := json.Unmarshal(raw, &cfg); err != nil {
			return cfg, fmt.Errorf("parse config %s: %w", path, err)
		}
	}
	applyEnv(&cfg)
	return cfg, cfg.Validate()
}

func applyEnv(cfg *Config) {
	if v := os.Getenv("DHCPV4LAB_UDP_LISTEN"); v != "" {
		cfg.Server.UDPListen = v
	}
	if v := os.Getenv("DHCPV4LAB_HTTP_LISTEN"); v != "" {
		cfg.Server.HTTPListen = v
	}
	if v := os.Getenv("DHCPV4LAB_DSN"); v != "" {
		cfg.Store.DSN = v
	}
	if v := os.Getenv("DHCPV4LAB_TEST_MODE"); v != "" {
		cfg.TestMode = v == "1" || strings.EqualFold(v, "true")
	}
	if v := os.Getenv("DHCPV4LAB_ALLOW_NON_LOOPBACK"); v != "" {
		cfg.Server.AllowNonLoopback = v == "1" || strings.EqualFold(v, "true")
	}
}

// Validate checks structural and semantic constraints and returns all errors.
func (c *Config) Validate() error {
	var problems []string
	add := func(f string, a ...any) { problems = append(problems, fmt.Sprintf(f, a...)) }

	if c.Server.UDPListen == "" {
		add("server.udpListen is required")
	} else if !c.Server.AllowNonLoopback {
		if err := assertLoopbackHostPort(c.Server.UDPListen); err != nil {
			add("server.udpListen: %v", err)
		}
	}
	if c.Server.HTTPListen == "" {
		add("server.httpListen is required")
	} else if !c.Server.AllowNonLoopback {
		if err := assertLoopbackHostPort(c.Server.HTTPListen); err != nil {
			add("server.httpListen: %v", err)
		}
	}

	start, err := parseV4(c.Pool.RangeStart)
	if err != nil {
		add("pool.rangeStart: %v", err)
	}
	end, err := parseV4(c.Pool.RangeEnd)
	if err != nil {
		add("pool.rangeEnd: %v", err)
	}
	if start != nil && end != nil {
		if ipToUint(*start) > ipToUint(*end) {
			add("pool.rangeStart %s must not exceed rangeEnd %s", c.Pool.RangeStart, c.Pool.RangeEnd)
		}
		if !c.Server.AllowNonLoopback && !(isLoopbackV4(*start) && isLoopbackV4(*end)) {
			add("pool range must be inside 127.0.0.0/8 while allowNonLoopback=false")
		}
		if ipToUint(*end)-ipToUint(*start)+1 > 65536 {
			add("pool range too large (max 65536 addresses in the lab)")
		}
	}
	if _, err := parseV4(c.Pool.ServerID); err != nil {
		add("pool.serverId: %v", err)
	}
	if c.Pool.Netmask == "" {
		add("pool.netmask is required")
	} else if _, err := parseV4(c.Pool.Netmask); err != nil {
		add("pool.netmask: %v", err)
	}
	if c.Pool.Router != "" {
		if _, err := parseV4(c.Pool.Router); err != nil {
			add("pool.router: %v", err)
		}
	}
	for i, d := range c.Pool.DNS {
		if _, err := parseV4(d); err != nil {
			add("pool.dns[%d]: %v", i, err)
		}
	}

	if c.Lease.LeaseTime.Duration <= 0 {
		add("lease.leaseTime must be positive")
	}
	if c.Lease.OfferTTL.Duration <= 0 {
		add("lease.offerTTL must be positive")
	}
	if c.Lease.T1.Duration >= c.Lease.LeaseTime.Duration {
		add("lease.t1 (%s) must be less than leaseTime (%s)", c.Lease.T1, c.Lease.LeaseTime)
	}
	if c.Lease.T2.Duration <= c.Lease.T1.Duration {
		add("lease.t2 (%s) must be greater than t1 (%s)", c.Lease.T2, c.Lease.T1)
	}
	if c.Lease.T2.Duration >= c.Lease.LeaseTime.Duration {
		add("lease.t2 (%s) must be less than leaseTime (%s)", c.Lease.T2, c.Lease.LeaseTime)
	}
	c.Options = OptionConfig{Netmask: c.Pool.Netmask, Router: c.Pool.Router, DNS: c.Pool.DNS}

	if c.Store.DSN == "" {
		add("store.dsn is required")
	}
	if len(problems) > 0 {
		return fmt.Errorf("invalid configuration:\n  - %s", strings.Join(problems, "\n  - "))
	}
	return nil
}

// LeaseSeconds returns the lease time in whole seconds for option 51.
func (c *Config) LeaseSeconds() uint32 { return uint32(c.Lease.LeaseTime.Seconds()) }

// T1Seconds/T2Seconds expose renewal timers as option values.
func (c *Config) T1Seconds() uint32 { return uint32(c.Lease.T1.Seconds()) }
func (c *Config) T2Seconds() uint32 { return uint32(c.Lease.T2.Seconds()) }

func assertLoopbackHostPort(hp string) error {
	ap, err := netip.ParseAddrPort(hp)
	if err != nil {
		return fmt.Errorf("%q is not a valid host:port: %w", hp, err)
	}
	if !ap.Addr().IsLoopback() {
		return fmt.Errorf("%s must be a loopback address (set allowNonLoopback=true to override at your own risk)", hp)
	}
	return nil
}
