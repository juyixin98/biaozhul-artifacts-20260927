// Package config parses and validates ribd configuration.
//
// Configuration is a single JSON document (see testdata/config/example.json).
// Parsing is strict: unknown keys are rejected, invalid ranges and cross-family
// routes are reported with the offending field path (e.g.
// "routes[3].admin_distance").
package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"strings"

	"github.com/opp221/ribd/internal/netmodel"
)

// Config is the full daemon configuration.
type Config struct {
	Server     ServerConfig     `json:"server"`
	Storage    StorageConfig    `json:"storage"`
	Resolution ResolutionConfig `json:"resolution"`
	Routes     []netmodel.Route `json:"routes,omitempty"`
}

// ServerConfig tunes the HTTP replay/query interface.
type ServerConfig struct {
	Listen         string `json:"listen"`
	ReadTimeoutMS  int    `json:"read_timeout_ms"`
	WriteTimeoutMS int    `json:"write_timeout_ms"`
}

// StorageConfig selects the state backend.
type StorageConfig struct {
	// Driver is "sqlite" (persisted file) or "memory".
	Driver string `json:"driver"`
	// DSN is the SQLite file path for driver=sqlite.
	DSN string `json:"dsn,omitempty"`
}

// ResolutionConfig controls recursive next-hop resolution.
type ResolutionConfig struct {
	MaxDepth int `json:"max_depth"`
}

// Defaults applied to absent fields.
func Defaults() Config {
	return Config{
		Server: ServerConfig{
			Listen:         "127.0.0.1:8080",
			ReadTimeoutMS:  5000,
			WriteTimeoutMS: 10000,
		},
		Storage: StorageConfig{Driver: "memory"},
		Resolution: ResolutionConfig{
			MaxDepth: 8,
		},
	}
}

// LoadFile reads, parses and validates a config file.
func LoadFile(path string) (Config, error) {
	f, err := os.Open(path)
	if err != nil {
		return Config{}, fmt.Errorf("config: open %s: %w", path, err)
	}
	defer f.Close()
	return Load(f)
}

// Load parses and validates a config stream.
func Load(r io.Reader) (Config, error) {
	cfg := Defaults()
	raw, err := io.ReadAll(r)
	if err != nil {
		return Config{}, fmt.Errorf("config: read: %w", err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		return Config{}, fmt.Errorf("config: parse: %w", err)
	}
	if dec.More() {
		return Config{}, fmt.Errorf("config: parse: unexpected trailing JSON content")
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func splitHostPort(listen string) (string, string, error) {
	host, port, err := net.SplitHostPort(listen)
	if err != nil {
		return "", "", err
	}
	if host == "" {
		return "", "", fmt.Errorf("missing host")
	}
	return host, port, nil
}

// Validate checks every field, reporting concrete paths.
func (c *Config) Validate() error {
	if _, _, err := splitHostPort(c.Server.Listen); err != nil {
		return fmt.Errorf("config: server.listen invalid: %w", err)
	}
	if c.Server.ReadTimeoutMS < 0 {
		return fmt.Errorf("config: server.read_timeout_ms must be >= 0")
	}
	if c.Server.WriteTimeoutMS < 0 {
		return fmt.Errorf("config: server.write_timeout_ms must be >= 0")
	}
	switch c.Storage.Driver {
	case "sqlite", "memory":
	default:
		return fmt.Errorf("config: storage.driver %q invalid (want sqlite|memory)", c.Storage.Driver)
	}
	if c.Storage.Driver == "sqlite" && strings.TrimSpace(c.Storage.DSN) == "" {
		return fmt.Errorf("config: storage.dsn required when driver=sqlite")
	}
	if c.Resolution.MaxDepth < 1 || c.Resolution.MaxDepth > 64 {
		return fmt.Errorf("config: resolution.max_depth %d out of range [1,64]", c.Resolution.MaxDepth)
	}
	seen := map[string]int{}
	for i := range c.Routes {
		r := &c.Routes[i]
		if r.ID == "" {
			return fmt.Errorf("config: routes[%d].id must not be empty", i)
		}
		if prev, dup := seen[r.ID]; dup {
			return fmt.Errorf("config: routes[%d].id %q duplicates routes[%d]", i, r.ID, prev)
		}
		seen[r.ID] = i
		if err := r.Validate(); err != nil {
			return fmt.Errorf("config: routes[%d]: %w", i, err)
		}
	}
	return nil
}
