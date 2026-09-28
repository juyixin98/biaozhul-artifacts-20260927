// Package config parses and validates the single JSON startup configuration.
// Defaults are applied here; downstream packages receive a fully-valid Config.
package config

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"time"

	"natlab/internal/model"
)

// Timeouts are per-state, per-protocol durations. TCP and UDP are separate.
type Timeouts struct {
	TCPSynSent     string `json:"tcp_syn_sent"`
	TCPTransient   string `json:"tcp_transient"` // syn_ack_rcvd and fin_wait
	TCPEstablished string `json:"tcp_established"`
	UDP            string `json:"udp"`
}

// Config is the complete startup configuration.
type Config struct {
	ListenAddr     string   `json:"listen_addr"`
	DBPath         string   `json:"db_path"`
	LogPath        string   `json:"log_path"` // empty => stderr
	PublicIP       string   `json:"public_ip"`
	PrivateCIDRs   []string `json:"private_cidrs"`
	PortLow        uint16   `json:"port_pool_low"`
	PortHigh       uint16   `json:"port_pool_high"`
	Timeouts       Timeouts `json:"timeouts"`
	CloseFreesPort bool     `json:"close_frees_port"` // RST / completed FIN closes immediately

	// Resolved fields, not serialized.
	publicIP     netip.Addr     `json:"-"`
	privateNets  []netip.Prefix `json:"-"`
	tcpSynSent   time.Duration  `json:"-"`
	tcpTransient time.Duration  `json:"-"`
	tcpEst       time.Duration  `json:"-"`
	udp          time.Duration  `json:"-"`
}

// Defaults returns a configuration usable for local experiments and tests.
func Defaults() *Config {
	return &Config{
		ListenAddr:   "127.0.0.1:8080",
		DBPath:       "./natlab.db",
		LogPath:      "",
		PublicIP:     "203.0.113.1",
		PrivateCIDRs: []string{"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"},
		PortLow:      40000,
		PortHigh:     60000,
		Timeouts: Timeouts{
			TCPSynSent:     "30s",
			TCPTransient:   "60s",
			TCPEstablished: "7440s", // 2h4m, RFC 5382's established baseline
			UDP:            "180s",  // 3 minutes
		},
		CloseFreesPort: true,
	}
}

// Load reads a JSON config file. An empty path yields Defaults.
func Load(path string) (*Config, error) {
	cfg := Defaults()
	if path != "" {
		b, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read config %q: %w", path, err)
		}
		if err := json.Unmarshal(b, cfg); err != nil {
			return nil, fmt.Errorf("parse config %q: %w", path, err)
		}
	}
	if err := cfg.Resolve(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Resolve validates the config and populates derived fields. It is exported so
// callers (e.g. the replay runner) can build a Config struct programmatically.
func (c *Config) Resolve() error { return c.resolve() }

func (c *Config) resolve() error {
	var err error
	if c.PublicIP == "" {
		return fmt.Errorf("public_ip is required")
	}
	if c.publicIP, err = netip.ParseAddr(c.PublicIP); err != nil {
		return fmt.Errorf("public_ip %q: %w", c.PublicIP, err)
	}
	if !c.publicIP.Is4() {
		return fmt.Errorf("public_ip %q must be IPv4", c.PublicIP)
	}
	if len(c.PrivateCIDRs) == 0 {
		return fmt.Errorf("private_cidrs must contain at least one prefix")
	}
	for _, s := range c.PrivateCIDRs {
		p, err := netip.ParsePrefix(s)
		if err != nil {
			return fmt.Errorf("private_cidrs %q: %w", s, err)
		}
		if !p.Addr().Is4() {
			return fmt.Errorf("private_cidrs %q must be IPv4", s)
		}
		c.privateNets = append(c.privateNets, p)
	}
	if c.PortLow == 0 || c.PortHigh == 0 || c.PortLow > c.PortHigh {
		return fmt.Errorf("port pool invalid: low=%d high=%d", c.PortLow, c.PortHigh)
	}
	if c.tcpSynSent, err = parseDur(c.Timeouts.TCPSynSent); err != nil {
		return fmt.Errorf("tcp_syn_sent: %w", err)
	}
	if c.tcpTransient, err = parseDur(c.Timeouts.TCPTransient); err != nil {
		return fmt.Errorf("tcp_transient: %w", err)
	}
	if c.tcpEst, err = parseDur(c.Timeouts.TCPEstablished); err != nil {
		return fmt.Errorf("tcp_established: %w", err)
	}
	if c.udp, err = parseDur(c.Timeouts.UDP); err != nil {
		return fmt.Errorf("udp: %w", err)
	}
	return nil
}

// Accessors keep the resolved fields package-hidden in spirit while sharing
// the Config value across packages.

// PublicAddr returns the external address the NAT uses.
func (c *Config) PublicAddr() netip.Addr { return c.publicIP }

// PublicIPString returns the external address in text form.
func (c *Config) PublicIPString() string { return c.publicIP.String() }

// IsPrivate reports whether addr belongs to a configured inside prefix.
func (c *Config) IsPrivate(addr netip.Addr) bool {
	for _, p := range c.privateNets {
		if p.Contains(addr) {
			return true
		}
	}
	return false
}

// TTL resolves the lifetime for a protocol/state pair.
func (c *Config) TTL(proto model.Protocol, state string) (d durationType, ok bool) {
	switch proto {
	case model.TCP:
		switch state {
		case model.StateSynSent:
			return c.tcpSynSent, true
		case model.StateSynAckRcvd, model.StateFinWait:
			return c.tcpTransient, true
		case model.StateEstablished:
			return c.tcpEst, true
		}
	case model.UDP:
		if state == model.StateOpen {
			return c.udp, true
		}
	}
	return 0, false
}
