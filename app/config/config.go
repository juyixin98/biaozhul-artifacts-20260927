// Package config loads server/worker configuration. Values come from a YAML
// file (./config/config.yaml by default) and are overridden by DSNET_*
// environment variables — no real accounts or external services are ever
// required; the defaults point at a local Unix-socket PostgreSQL peer login.
package config

import (
	"fmt"
	"os"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

type Config struct {
	HTTPAddr      string        `yaml:"http_addr"`
	DatabaseDSN   string        `yaml:"database_dsn"`
	WorkerTTL     time.Duration `yaml:"worker_ttl"`
	SweepInterval time.Duration `yaml:"sweep_interval"`
	PollInterval  time.Duration `yaml:"poll_interval"`
	Heartbeat     time.Duration `yaml:"heartbeat_interval"`
	TrailEntries  int           `yaml:"trail_entries"`
}

// Default returns the fully local defaults.
func Default() Config {
	return Config{
		HTTPAddr:      "127.0.0.1:18080",
		DatabaseDSN:   "host=/var/run/postgresql user=admin dbname=dsnet sslmode=disable",
		WorkerTTL:     5 * time.Second,
		SweepInterval: 50 * time.Millisecond,
		PollInterval:  40 * time.Millisecond,
		Heartbeat:     1 * time.Second,
		TrailEntries:  12,
	}
}

// Load reads path if present (missing file is OK when path=="" or default),
// then applies DSNET_* overrides.
func Load(path string) (Config, error) {
	cfg := Default()
	if path == "" {
		path = os.Getenv("DSNET_CONFIG")
	}
	if path != "" {
		b, err := os.ReadFile(path)
		if err != nil {
			if !os.IsNotExist(err) {
				return cfg, fmt.Errorf("read config: %w", err)
			}
		} else {
			if err := yaml.Unmarshal(b, &cfg); err != nil {
				return cfg, fmt.Errorf("parse config: %w", err)
			}
		}
	}
	if v := os.Getenv("DSNET_HTTP_ADDR"); v != "" {
		cfg.HTTPAddr = v
	}
	if v := os.Getenv("DSNET_DATABASE_DSN"); v != "" {
		cfg.DatabaseDSN = v
	}
	if v := os.Getenv("DSNET_WORKER_TTL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return cfg, fmt.Errorf("DSNET_WORKER_TTL: %w", err)
		}
		cfg.WorkerTTL = d
	}
	if v := os.Getenv("DSNET_SWEEP_INTERVAL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return cfg, fmt.Errorf("DSNET_SWEEP_INTERVAL: %w", err)
		}
		cfg.SweepInterval = d
	}
	return cfg, nil
}

// RedactedDSN hides anything password-ish for logs (the local default has
// none, but we never print credentials on principle).
func (c Config) RedactedDSN() string {
	d := c.DatabaseDSN
	for _, k := range []string{"password", "pass", "pwd"} {
		if i := strings.Index(strings.ToLower(d), k+"="); i >= 0 {
			return d[:i] + k+"=<redacted>"
		}
	}
	return d
}
