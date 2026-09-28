// Package config loads service configuration from JSON files with explicit
// environment-variable overrides. Unknown JSON keys are rejected so that a
// typo in the config never silently changes nothing.
package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
)

// Config is the complete service configuration.
type Config struct {
	// Listen is the HTTP bind address, e.g. "127.0.0.1:8080".
	Listen string `json:"listen"`
	// DBPath is the SQLite database file; ":memory:" is supported for tests.
	DBPath string `json:"db_path"`
	// MaxEntriesPerList bounds allow/exclude list length per request.
	MaxEntriesPerList int `json:"max_entries_per_list"`
	// ShutdownTimeoutMS bounds graceful shutdown.
	ShutdownTimeoutMS int `json:"shutdown_timeout_ms"`
}

// Default returns the built-in defaults; every field has a sane value.
func Default() Config {
	return Config{
		Listen:            "127.0.0.1:8080",
		DBPath:            "cidrcov.db",
		MaxEntriesPerList: 10_000,
		ShutdownTimeoutMS: 5_000,
	}
}

// Load reads path (if non-empty and present), applies defaults first and
// CIDRCOV_* environment overrides last, then validates.
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		raw, err := os.ReadFile(path)
		if err != nil {
			return cfg, fmt.Errorf("read config %s: %w", path, err)
		}
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&cfg); err != nil {
			return cfg, fmt.Errorf("parse config %s: %w", path, err)
		}
	}
	applyEnv(&cfg)
	if err := cfg.Validate(); err != nil {
		return cfg, err
	}
	return cfg, nil
}

func applyEnv(cfg *Config) {
	if v, ok := os.LookupEnv("CIDRCOV_LISTEN"); ok {
		cfg.Listen = v
	}
	if v, ok := os.LookupEnv("CIDRCOV_DB_PATH"); ok {
		cfg.DBPath = v
	}
	if v, ok := os.LookupEnv("CIDRCOV_MAX_ENTRIES"); ok {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.MaxEntriesPerList = n
		}
	}
	if v, ok := os.LookupEnv("CIDRCOV_SHUTDOWN_MS"); ok {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.ShutdownTimeoutMS = n
		}
	}
}

// Validate rejects unusable values explicitly instead of failing later at use.
func (c Config) Validate() error {
	if c.Listen == "" {
		return fmt.Errorf("listen must not be empty")
	}
	if c.DBPath == "" {
		return fmt.Errorf("db_path must not be empty (use :memory: for ephemeral storage)")
	}
	if c.MaxEntriesPerList <= 0 {
		return fmt.Errorf("max_entries_per_list must be > 0, got %d", c.MaxEntriesPerList)
	}
	if c.ShutdownTimeoutMS <= 0 {
		return fmt.Errorf("shutdown_timeout_ms must be > 0, got %d", c.ShutdownTimeoutMS)
	}
	return nil
}
