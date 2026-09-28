// Package config loads cgcoordd configuration from a JSON file with
// environment variable overrides. It contains no coordination logic.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is the daemon configuration.
type Config struct {
	// HTTPAddr is the listen address, e.g. "127.0.0.1:8080".
	HTTPAddr string `json:"http_addr"`
	// Backend selects the state backend: "memory" or "postgres".
	Backend string `json:"backend"`
	// PostgresDSN is used when Backend == "postgres".
	PostgresDSN string `json:"postgres_dsn"`
	// SweepEvery is the background sweep interval ("0s" = disabled; the demo
	// drives sweeps explicitly so timing is deterministic).
	SweepInterval time.Duration `json:"-"`
	// SweepIntervalMS is the JSON form of SweepInterval.
	SweepIntervalMS int `json:"sweep_interval_ms"`
	// DefaultGroup timeouts applied when a group is created without values.
	DefaultGroup GroupDefaults `json:"default_group"`
}

// GroupDefaults are the default timeouts for new groups.
type GroupDefaults struct {
	RevokeTimeoutMS         int `json:"revoke_timeout_ms"`
	QuarantineTimeoutMS     int `json:"quarantine_timeout_ms"`
	DefaultSessionTimeoutMS int `json:"default_session_timeout_ms"`
}

// Default returns the demo-friendly defaults: memory backend, ephemeral
// address and no background sweep.
func Default() Config {
	return Config{
		HTTPAddr:  "127.0.0.1:8080",
		Backend:   "memory",
		SweepInterval: 0,
		DefaultGroup: GroupDefaults{
			RevokeTimeoutMS:         10_000,
			QuarantineTimeoutMS:     30_000,
			DefaultSessionTimeoutMS: 15_000,
		},
	}
}

// Load reads an optional JSON file (path may be "") and then applies
// CGCOORD_* environment overrides.
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		data, err := os.ReadFile(path)
		if err != nil {
			return cfg, fmt.Errorf("config: read %s: %w", path, err)
		}
		if err := json.Unmarshal(data, &cfg); err != nil {
			return cfg, fmt.Errorf("config: parse %s: %w", path, err)
		}
	}
	cfg.SweepInterval = time.Duration(cfg.SweepIntervalMS) * time.Millisecond

	apply := func(env string, set func(string)) {
		if v, ok := os.LookupEnv(env); ok && v != "" {
			set(v)
		}
	}
	apply("CGCOORD_HTTP_ADDR", func(v string) { cfg.HTTPAddr = v })
	apply("CGCOORD_BACKEND", func(v string) { cfg.Backend = strings.ToLower(v) })
	apply("CGCOORD_POSTGRES_DSN", func(v string) { cfg.PostgresDSN = v })
	apply("CGCOORD_SWEEP_INTERVAL_MS", func(v string) {
		if ms, err := strconv.Atoi(v); err == nil {
			cfg.SweepIntervalMS = ms
			cfg.SweepInterval = time.Duration(ms) * time.Millisecond
		}
	})

	if cfg.Backend != "memory" && cfg.Backend != "postgres" {
		return cfg, fmt.Errorf("config: backend must be memory|postgres, got %q", cfg.Backend)
	}
	if cfg.Backend == "postgres" && strings.TrimSpace(cfg.PostgresDSN) == "" {
		return cfg, fmt.Errorf("config: postgres backend requires postgres_dsn / CGCOORD_POSTGRES_DSN")
	}
	return cfg, nil
}
