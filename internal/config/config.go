// Package config parses and validates service configuration (timing
// constants from RFC 2236, simulated interfaces, HTTP/storage settings).
package config

import (
	"encoding/json"
	"fmt"
	"os"
)

// Timing holds the IGMPv2 router-side timers. Defaults are the RFC 2236
// §8 values, expressed in milliseconds; scenario fixtures may override them.
type Timing struct {
	// QueryInterval is the periodic General Query interval (RFC QI,
	// default 125s).
	QueryInterval int64 `json:"query_interval_ms"`
	// QueryResponseInterval is the Max Resp Time in periodic General
	// Queries (RFC QRI, default 10s).
	QueryResponseInterval int64 `json:"query_response_interval_ms"`
	// GroupMembershipInterval = QI + QRI (RFC GMI, default 260s when QI is
	// 250s in RFC §9 robustness discussion; RFC 2236 §8 derives
	// GMI = QRI*robustness + QI). Here it is configured directly so tests
	// can reason about it; default = QI + QRI.
	GroupMembershipInterval int64 `json:"group_membership_interval_ms"`
	// LastMemberQueryInterval is the Max Resp Time in group-specific
	// queries and the spacing between them (LMQI, default 1s).
	LastMemberQueryInterval int64 `json:"last_member_query_interval_ms"`
	// LastMemberQueryCount is the number of group-specific queries sent
	// before deleting the group (LMQC, default 2).
	LastMemberQueryCount int `json:"last_member_query_count"`
}

// Interface is one simulated router interface (a synthetic LAN segment).
type Interface struct {
	Name    string `json:"name"`
	Subnet  string `json:"subnet"` // informational, e.g. 192.0.2.0/24
	Comment string `json:"comment,omitempty"`
}

// Config is the root service configuration.
type Config struct {
	ServiceName string      `json:"service_name"`
	HTTPAddr    string      `json:"http_addr"`
	SQLitePath  string      `json:"sqlite_path"`
	Timing      Timing      `json:"timing"`
	Interfaces  []Interface `json:"interfaces"`
}

// Default returns the RFC 2236 §8 default timing configuration.
func Default() Config {
	return Config{
		ServiceName: "igmpv2-offline-timer",
		HTTPAddr:    "127.0.0.1:8022",
		SQLitePath:  "data/igmpv2.sqlite",
		Timing: Timing{
			QueryInterval:           125_000,
			QueryResponseInterval:   10_000,
			GroupMembershipInterval: 135_000, // QI + QRI
			LastMemberQueryInterval: 1_000,
			LastMemberQueryCount:    2,
		},
		Interfaces: []Interface{{Name: "eth0", Subnet: "192.0.2.0/24"}},
	}
}

// Load reads a JSON config file. An empty path yields Default().
func Load(path string) (Config, error) {
	cfg := Default()
	if path == "" {
		return cfg, cfg.Validate()
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config %s: %w", path, err)
	}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return cfg, fmt.Errorf("parse config %s: %w", path, err)
	}
	return cfg, cfg.Validate()
}

// Validate checks structural and RFC-derived constraints.
func (c *Config) Validate() error {
	t := c.Timing
	if t.QueryInterval <= 0 {
		return fmt.Errorf("timing.query_interval_ms must be > 0 (got %d)", t.QueryInterval)
	}
	if t.QueryResponseInterval <= 0 {
		return fmt.Errorf("timing.query_response_interval_ms must be > 0")
	}
	if t.QueryResponseInterval >= t.QueryInterval {
		return fmt.Errorf("timing.query_response_interval_ms (%d) must be < query_interval_ms (%d)",
			t.QueryResponseInterval, t.QueryInterval)
	}
	if t.GroupMembershipInterval <= 0 {
		return fmt.Errorf("timing.group_membership_interval_ms must be > 0")
	}
	// Note: in RFC 2236 deployments GMI is typically QI+QRI and therefore
	// larger than QI, but the simulator deliberately allows independent
	// values so fixtures can, for example, disable periodic queries with a
	// huge QI while exercising a short Group Membership Interval.
	if t.LastMemberQueryInterval <= 0 {
		return fmt.Errorf("timing.last_member_query_interval_ms must be > 0")
	}
	if t.LastMemberQueryCount < 1 {
		return fmt.Errorf("timing.last_member_query_count must be >= 1")
	}
	if c.HTTPAddr == "" {
		return fmt.Errorf("http_addr must not be empty")
	}
	names := map[string]bool{}
	for i, ifc := range c.Interfaces {
		if ifc.Name == "" {
			return fmt.Errorf("interfaces[%d].name must not be empty", i)
		}
		if names[ifc.Name] {
			return fmt.Errorf("interfaces[%d]: duplicate interface name %q", i, ifc.Name)
		}
		names[ifc.Name] = true
	}
	if len(c.Interfaces) == 0 {
		return fmt.Errorf("at least one interface must be configured")
	}
	return nil
}

// HasInterface reports whether the configured interfaces include name.
func (c *Config) HasInterface(name string) bool {
	for _, ifc := range c.Interfaces {
		if ifc.Name == name {
			return true
		}
	}
	return false
}

// MergeOverlay returns a copy of c with non-zero fields of o applied
// (scenario-level timing overrides).
func (t Timing) MergeOverlay(o Timing) Timing {
	out := t
	if o.QueryInterval != 0 {
		out.QueryInterval = o.QueryInterval
	}
	if o.QueryResponseInterval != 0 {
		out.QueryResponseInterval = o.QueryResponseInterval
	}
	if o.GroupMembershipInterval != 0 {
		out.GroupMembershipInterval = o.GroupMembershipInterval
	}
	if o.LastMemberQueryInterval != 0 {
		out.LastMemberQueryInterval = o.LastMemberQueryInterval
	}
	if o.LastMemberQueryCount != 0 {
		out.LastMemberQueryCount = o.LastMemberQueryCount
	}
	return out
}
