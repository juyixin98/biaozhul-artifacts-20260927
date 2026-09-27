// Package config parses and validates the standalone routing configuration.
//
// Configuration is JSON (stdlib encoding/json only):
//
//	{
//	  "listen_addr": "127.0.0.1:8080",
//	  "sqlite_path": "data/flexhash.db",
//	  "bucket_count": 1024,
//	  "members": [
//	    {"id": "hop-a", "address": "127.0.0.1:9001", "weight": 3, "healthy": true},
//	    {"id": "hop-b", "address": "127.0.0.1:9002", "weight": 1, "healthy": true}
//	  ]
//	}
package config

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"sort"

	"flexhash/internal/fherr"
)

// Limits guard against degenerate or hostile configs (resource exhaustion
// prevention at the parsing boundary).
const (
	MaxConfigBytes = 4 << 20 // 4 MiB
	MaxMembers     = 4096
	MinBucketCount = 1
	MaxBucketCount = 1_000_000
	MaxMemberIDLen = 128
	MaxWeight      = 1_000_000
	MaxAddressLen  = 256
)

// Member is one configured next hop.
type Member struct {
	ID      string `json:"id"`
	Address string `json:"address"`
	// Weight 0 is legal: the member is retained for configuration purposes
	// but receives neither bucket share nor failover traffic.
	Weight int `json:"weight"`
	// Healthy is the initial health; it is immediately overridden by stored
	// health state when booting from an existing database.
	Healthy bool `json:"healthy"`
}

// Config is the full service configuration.
type Config struct {
	ListenAddr  string   `json:"listen_addr"`
	SQLitePath  string   `json:"sqlite_path"`
	BucketCount int      `json:"bucket_count"`
	Members     []Member `json:"members"`
}

// LoadFile reads, parses and validates the configuration at path.
func LoadFile(path string) (*Config, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, fherr.Wrap(fherr.KindInput, "config.LoadFile", "config file not found: "+path, err)
		}
		return nil, fherr.Wrap(fherr.KindInput, "config.LoadFile", "cannot open config: "+path, err)
	}
	defer f.Close()
	return Parse(io.LimitReader(f, MaxConfigBytes+1))
}

// Parse reads JSON from r and validates it.
func Parse(r io.Reader) (*Config, error) {
	var c Config
	dec := json.NewDecoder(r)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return nil, fherr.Wrap(fherr.KindInput, "config.Parse", "invalid JSON: "+err.Error(), err)
	}
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return &c, nil
}

// Validate enforces every structural rule independently so that callers
// receive the precise failure class (input) and reason.
func (c *Config) Validate() error {
	const op = "config.Validate"
	if c == nil {
		return fherr.New(fherr.KindInput, op, "nil config")
	}
	if c.ListenAddr == "" {
		return fherr.New(fherr.KindInput, op, "listen_addr is required")
	}
	if _, err := net.ResolveTCPAddr("tcp", c.ListenAddr); err != nil {
		return fherr.Wrap(fherr.KindInput, op, "invalid listen_addr: "+c.ListenAddr, err)
	}
	if c.SQLitePath == "" {
		return fherr.New(fherr.KindInput, op, "sqlite_path is required")
	}
	if c.BucketCount < MinBucketCount || c.BucketCount > MaxBucketCount {
		return fherr.New(fherr.KindInput, op,
			fmt.Sprintf("bucket_count %d out of range [%d,%d]", c.BucketCount, MinBucketCount, MaxBucketCount))
	}
	if len(c.Members) == 0 {
		return fherr.New(fherr.KindInput, op, "at least one member is required")
	}
	if len(c.Members) > MaxMembers {
		return fherr.New(fherr.KindInput, op,
			fmt.Sprintf("member count %d exceeds limit %d", len(c.Members), MaxMembers))
	}
	seen := make(map[string]struct{}, len(c.Members))
	for i, m := range c.Members {
		at := func(msg string) error {
			return fherr.New(fherr.KindInput, op, fmt.Sprintf("members[%d] (%q): %s", i, m.ID, msg))
		}
		if m.ID == "" {
			return at("id is required")
		}
		if len(m.ID) > MaxMemberIDLen {
			return at(fmt.Sprintf("id length %d exceeds %d", len(m.ID), MaxMemberIDLen))
		}
		if _, dup := seen[m.ID]; dup {
			return at("duplicate member id")
		}
		seen[m.ID] = struct{}{}
		if m.Address == "" {
			return at("address is required")
		}
		if len(m.Address) > MaxAddressLen {
			return at(fmt.Sprintf("address length %d exceeds %d", len(m.Address), MaxAddressLen))
		}
		if _, err := net.ResolveTCPAddr("tcp", m.Address); err != nil {
			return fherr.Wrap(fherr.KindInput, op,
				fmt.Sprintf("members[%d] (%q): invalid address %q", i, m.ID, m.Address), err)
		}
		if m.Weight < 0 {
			return at("weight must be >= 0")
		}
		if m.Weight > MaxWeight {
			return at(fmt.Sprintf("weight %d exceeds %d", m.Weight, MaxWeight))
		}
	}
	return nil
}

// ValidateMembers validates just a member set (used by the topology-update
// endpoint, which does not resend listen_addr/sqlite_path).
func ValidateMembers(members []Member, bucketCount int) error {
	const op = "config.ValidateMembers"
	if len(members) == 0 {
		return fherr.New(fherr.KindInput, op, "at least one member is required")
	}
	if len(members) > MaxMembers {
		return fherr.New(fherr.KindInput, op,
			fmt.Sprintf("member count %d exceeds limit %d", len(members), MaxMembers))
	}
	if bucketCount < MinBucketCount || bucketCount > MaxBucketCount {
		return fherr.New(fherr.KindInput, op,
			fmt.Sprintf("bucket_count %d out of range [%d,%d]", bucketCount, MinBucketCount, MaxBucketCount))
	}
	seen := make(map[string]struct{}, len(members))
	for i, m := range members {
		at := func(msg string) error {
			return fherr.New(fherr.KindInput, op, fmt.Sprintf("members[%d] (%q): %s", i, m.ID, msg))
		}
		if m.ID == "" {
			return at("id is required")
		}
		if len(m.ID) > MaxMemberIDLen {
			return at(fmt.Sprintf("id length %d exceeds %d", len(m.ID), MaxMemberIDLen))
		}
		if _, dup := seen[m.ID]; dup {
			return at("duplicate member id")
		}
		seen[m.ID] = struct{}{}
		if m.Address == "" {
			return at("address is required")
		}
		if len(m.Address) > MaxAddressLen {
			return at(fmt.Sprintf("address length %d exceeds %d", len(m.Address), MaxAddressLen))
		}
		if _, err := net.ResolveTCPAddr("tcp", m.Address); err != nil {
			return fherr.Wrap(fherr.KindInput, op,
				fmt.Sprintf("members[%d] (%q): invalid address %q", i, m.ID, m.Address), err)
		}
		if m.Weight < 0 {
			return at("weight must be >= 0")
		}
		if m.Weight > MaxWeight {
			return at(fmt.Sprintf("weight %d exceeds %d", m.Weight, MaxWeight))
		}
	}
	return nil
}

// SortedMembers returns the members sorted by ID, the canonical deterministic
// order used for tie-breaking and storage.
func (c *Config) SortedMembers() []Member {
	out := make([]Member, len(c.Members))
	copy(out, c.Members)
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// MemberByID looks up a member by ID.
func (c *Config) MemberByID(id string) (Member, bool) {
	for _, m := range c.Members {
		if m.ID == id {
			return m, true
		}
	}
	return Member{}, false
}
