// Package config loads runtime configuration from a JSON file and environment
// variable overrides. Parsing is strict: unknown keys and invalid values fail
// loudly instead of being silently ignored.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is the resolved service configuration.
type Config struct {
	// HTTPListen is the bind address, e.g. "127.0.0.1:8080".
	HTTPListen string `json:"http_listen"`
	// DatabasePath is the SQLite file. The special value ":memory:" uses a
	// private in-memory database (tests, ephemeral runs).
	DatabasePath string `json:"database_path"`
	// LogPath: "-" or "" writes structured logs to stderr; a path appends to
	// that file.
	LogPath string `json:"log_path"`
	// MaxInputPrefixes bounds a single request's allow/exclude list length.
	MaxInputPrefixes int `json:"max_input_prefixes"`
	// ShutdownTimeout bounds graceful shutdown.
	ShutdownTimeout time.Duration `json:"-"`
	ShutdownRaw     string        `json:"shutdown_timeout"`
	// StrictCIDR rejects host bits in request CIDRs when true.
	StrictCIDR bool `json:"strict_cidr"`
}

// Default returns the built-in defaults.
func Default() Config {
	return Config{
		HTTPListen:       "127.0.0.1:8080",
		DatabasePath:     "data/cidrsvc.db",
		LogPath:          "-",
		MaxInputPrefixes: 4096,
		ShutdownTimeout:  5 * time.Second,
		ShutdownRaw:      "5s",
		StrictCIDR:       false,
	}
}

// Load reads path (when non-empty) as JSON and applies environment overrides
// CIDRSVC_<FIELD>. Missing files are reported, not replaced by defaults, so a
// deployment typo never silently falls back.
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		raw, err := os.ReadFile(path)
		if err != nil {
			return Config{}, fmt.Errorf("read config %q: %w", path, err)
		}
		dec := json.NewDecoder(strings.NewReader(string(raw)))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&cfg); err != nil {
			return Config{}, fmt.Errorf("parse config %q: %w", path, err)
		}
	}

	if err := applyEnv(&cfg); err != nil {
		return Config{}, err
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func applyEnv(cfg *Config) error {
	var err error
	set := func(env, field, value string, assign func(string) error) {
		if err != nil {
			return
		}
		if v, ok := os.LookupEnv(env); ok {
			if e := assign(v); e != nil {
				err = fmt.Errorf("env %s=%q: %w", env, v, e)
			}
			_ = field
		}
	}
	set("CIDRSVC_HTTP_LISTEN", "http_listen", "", func(v string) error { cfg.HTTPListen = v; return nil })
	set("CIDRSVC_DB_PATH", "database_path", "", func(v string) error { cfg.DatabasePath = v; return nil })
	set("CIDRSVC_LOG_PATH", "log_path", "", func(v string) error { cfg.LogPath = v; return nil })
	set("CIDRSVC_MAX_INPUT_PREFIXES", "max_input_prefixes", "", func(v string) error {
		n, e := strconv.Atoi(v)
		if e != nil || n < 1 {
			return fmt.Errorf("must be a positive integer")
		}
		cfg.MaxInputPrefixes = n
		return nil
	})
	set("CIDRSVC_STRICT_CIDR", "strict_cidr", "", func(v string) error {
		b, e := strconv.ParseBool(v)
		if e != nil {
			return e
		}
		cfg.StrictCIDR = b
		return nil
	})
	set("CIDRSVC_SHUTDOWN_TIMEOUT", "shutdown_timeout", "", func(v string) error {
		cfg.ShutdownRaw = v
		return nil
	})
	return err
}

// Validate checks cross-field constraints and normalises derived values.
func (c *Config) Validate() error {
	if c.HTTPListen == "" {
		return fmt.Errorf("http_listen must not be empty")
	}
	if c.DatabasePath == "" {
		return fmt.Errorf("database_path must not be empty")
	}
	if c.MaxInputPrefixes < 1 {
		return fmt.Errorf("max_input_prefixes must be >= 1, got %d", c.MaxInputPrefixes)
	}
	d, err := time.ParseDuration(c.ShutdownRaw)
	if err != nil {
		return fmt.Errorf("shutdown_timeout %q: %w", c.ShutdownRaw, err)
	}
	if d <= 0 {
		return fmt.Errorf("shutdown_timeout must be positive")
	}
	c.ShutdownTimeout = d
	return nil
}

// String renders the effective configuration for startup logging (no secrets
// exist in this service, but DB paths are still useful on one line).
func (c Config) String() string {
	return fmt.Sprintf("listen=%s db=%s log=%s max_prefixes=%d strict=%v shutdown=%s",
		c.HTTPListen, c.DatabasePath, c.LogPath, c.MaxInputPrefixes, c.StrictCIDR, c.ShutdownRaw)
}
