// Package config loads and validates the reassembly service configuration.
//
// The configuration layer is deliberately separate from the network model
// and the reassembly engine so that tests can construct small configs
// directly while the server/CLI load them from JSON files.
package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"time"
)

// Duration is a time.Duration that unmarshals from JSON strings like "30s".
type Duration struct{ time.Duration }

func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return fmt.Errorf("duration must be a JSON string such as \"30s\": %w", err)
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return fmt.Errorf("invalid duration %q: %w", s, err)
	}
	d.Duration = v
	return nil
}

func (d Duration) MarshalJSON() ([]byte, error) { return json.Marshal(d.String()) }

// Config groups every tunable of the reassembly backend.
type Config struct {
	// Timeout is the per-datagram reassembly timeout: a group that sees no
	// fragment for longer than this is expired and its state reclaimed.
	Timeout Duration `json:"timeout"`
	// SweepInterval is how often the server expires idle groups when the
	// input stream does not drive the clock (live JSON fragment mode).
	SweepInterval Duration `json:"sweep_interval"`
	// MaxDatagramSize caps offset*8+len(payload); anything beyond is
	// rejected as oversize. RFC 791 bounds this at 65535.
	MaxDatagramSize int `json:"max_datagram_size"`
	// MaxDatagrams bounds concurrently tracked groups (capacity guard).
	MaxDatagrams int `json:"max_datagrams"`
	// MaxBufferedBytes bounds total payload bytes buffered across groups.
	MaxBufferedBytes int64 `json:"max_buffered_bytes"`
	// DBPath is the SQLite file used for state storage and the audit log.
	DBPath string `json:"db_path"`
	// Listen is the HTTP listen address of the replay server.
	Listen string `json:"listen"`
}

// Default returns the production-default configuration.
func Default() Config {
	return Config{
		Timeout:          Duration{30 * time.Second},
		SweepInterval:    Duration{5 * time.Second},
		MaxDatagramSize:  65535,
		MaxDatagrams:     10000,
		MaxBufferedBytes: 64 << 20,
		DBPath:           "reasm.db",
		Listen:           "127.0.0.1:8080",
	}
}

// Load reads a JSON config file over the defaults. Unknown fields are
// rejected so typos cannot silently disable a safety limit.
func Load(path string) (Config, error) {
	cfg := Default()
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config %s: %w", path, err)
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		return cfg, fmt.Errorf("parse config %s: %w", path, err)
	}
	if err := cfg.Validate(); err != nil {
		return cfg, fmt.Errorf("invalid config %s: %w", path, err)
	}
	return cfg, nil
}

// Validate enforces the invariants the reassembly engine relies on.
func (c Config) Validate() error {
	if c.Timeout.Duration <= 0 {
		return fmt.Errorf("timeout must be positive, got %s", c.Timeout.Duration)
	}
	if c.SweepInterval.Duration <= 0 {
		return fmt.Errorf("sweep_interval must be positive, got %s", c.SweepInterval.Duration)
	}
	if c.MaxDatagramSize < 576 || c.MaxDatagramSize > 65535 {
		return fmt.Errorf("max_datagram_size must be within [576, 65535], got %d", c.MaxDatagramSize)
	}
	if c.MaxDatagrams <= 0 {
		return fmt.Errorf("max_datagrams must be positive, got %d", c.MaxDatagrams)
	}
	if c.MaxBufferedBytes <= 0 {
		return fmt.Errorf("max_buffered_bytes must be positive, got %d", c.MaxBufferedBytes)
	}
	if c.DBPath == "" {
		return fmt.Errorf("db_path must not be empty")
	}
	return nil
}
