// Package config loads runtime configuration from flags/env with explicit
// local-only defaults.
package config

import (
	"flag"
	"os"
	"strconv"
	"time"
)

// Config is the process configuration.
type Config struct {
	HTTPAddr    string
	DBPath      string
	TickPeriod  time.Duration
	ManualTicks bool
	SimCapacity int
}

// Parse builds Config from flags with env overrides.
func Parse(args []string) (Config, error) {
	fs := flag.NewFlagSet("rollctl", flag.ContinueOnError)
	cfg := Config{}
	fs.StringVar(&cfg.HTTPAddr, "http", envOr("ROLLCTL_HTTP", "127.0.0.1:8080"), "HTTP listen address")
	fs.StringVar(&cfg.DBPath, "db", envOr("ROLLCTL_DB", "./data/rollctl.db"), "SQLite database path")
	fs.DurationVar(&cfg.TickPeriod, "tick", durationEnv("ROLLCTL_TICK", 500*time.Millisecond), "auto reconcile period (0 disables auto loop)")
	fs.BoolVar(&cfg.ManualTicks, "manual", envOr("ROLLCTL_MANUAL", "") == "1", "manual tick mode: only POST /admin/tick advances state")
	fs.IntVar(&cfg.SimCapacity, "sim-capacity", intEnv("ROLLCTL_SIM_CAPACITY", 16), "simulated process-manager capacity")
	if err := fs.Parse(args); err != nil {
		return cfg, err
	}
	if cfg.ManualTicks {
		cfg.TickPeriod = 0
	}
	return cfg, nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func intEnv(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func durationEnv(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
