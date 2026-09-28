// Package config loads and validates broker runtime configuration from the
// environment (12-factor style). It is a separate layer so tests construct
// values directly and production deployments override via the environment.
package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config is the validated process configuration.
type Config struct {
	// HTTPAddr is the listen address of the HTTP API.
	HTTPAddr string
	// Driver selects the state store: "mem" (default, zero dependencies) or
	// "postgres".
	Driver string
	// PostgresDSN is used when Driver=postgres.
	PostgresDSN string
	// SweepInterval drives background lease expiry. Zero disables the
	// background sweeper (tests drive ExpireDue manually).
	SweepInterval time.Duration
	// RunID tags every event emitted by this process; defaults to the PID so
	// logs can be correlated to a concrete run.
	RunID string
}

// Default returns process defaults suitable for a local run.
func Default() Config {
	return Config{
		HTTPAddr:      ":8080",
		Driver:        "mem",
		PostgresDSN:   "host=/var/run/postgresql user=admin dbname=brokertest sslmode=disable",
		SweepInterval: 200 * time.Millisecond,
		RunID:         "",
	}
}

// FromEnv overlays BROKER_* environment variables on top of base. Unknown
// drivers and malformed durations produce explicit errors rather than silent
// fallback.
func FromEnv(base Config) (Config, error) {
	cfg := base
	var err error
	if v := os.Getenv("BROKER_HTTP_ADDR"); v != "" {
		cfg.HTTPAddr = v
	}
	if v := os.Getenv("BROKER_DRIVER"); v != "" {
		if v != "mem" && v != "postgres" {
			return cfg, fmt.Errorf("BROKER_DRIVER must be mem or postgres, got %q", v)
		}
		cfg.Driver = v
	}
	if v := os.Getenv("BROKER_POSTGRES_DSN"); v != "" {
		cfg.PostgresDSN = v
	}
	if v := os.Getenv("BROKER_SWEEP_INTERVAL"); v != "" {
		cfg.SweepInterval, err = time.ParseDuration(v)
		if err != nil {
			return cfg, fmt.Errorf("BROKER_SWEEP_INTERVAL: %w", err)
		}
		if cfg.SweepInterval < 0 {
			return cfg, fmt.Errorf("BROKER_SWEEP_INTERVAL must be >= 0")
		}
	}
	if v := os.Getenv("BROKER_RUN_ID"); v != "" {
		cfg.RunID = v
	}
	if cfg.Driver == "postgres" && cfg.PostgresDSN == "" {
		return cfg, fmt.Errorf("BROKER_POSTGRES_DSN required when driver=postgres")
	}
	return cfg, nil
}

// MustInt is a small helper kept for callers building synthetic configs in
// examples/tests.
func MustInt(s string) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		panic(err)
	}
	return n
}
