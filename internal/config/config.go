// Package config loads run configuration. There is no external config service:
// one small YAML-free JSON file plus environment overrides keeps the project
// reproducible using only the standard library.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"time"
)

// Config is the full server configuration.
type Config struct {
	HTTPAddr      string `json:"http_addr"`
	DatabasePath  string `json:"database_path"`
	ApprovalTTLMS int64  `json:"approval_ttl_ms"`
	SweepEveryMS  int64  `json:"sweep_interval_ms"`
}

// Default returns the local development defaults.
func Default() Config {
	return Config{
		HTTPAddr:      ":8080",
		DatabasePath:  "./data/coordinator.db",
		ApprovalTTLMS: 30_000,
		SweepEveryMS:  5_000,
	}
}

// Load reads path if it exists, applies defaults, then environment overrides:
// EC_HTTP_ADDR, EC_DB_PATH, EC_APPROVAL_TTL_MS.
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		b, err := os.ReadFile(path)
		if err != nil {
			if !os.IsNotExist(err) {
				return cfg, fmt.Errorf("read config: %w", err)
			}
		} else {
			if err := json.Unmarshal(b, &cfg); err != nil {
				return cfg, fmt.Errorf("parse config %s: %w", path, err)
			}
		}
	}
	if v := os.Getenv("EC_HTTP_ADDR"); v != "" {
		cfg.HTTPAddr = v
	}
	if v := os.Getenv("EC_DB_PATH"); v != "" {
		cfg.DatabasePath = v
	}
	if v := os.Getenv("EC_APPROVAL_TTL_MS"); v != "" {
		var ms int64
		if _, err := fmt.Sscanf(v, "%d", &ms); err != nil {
			return cfg, fmt.Errorf("EC_APPROVAL_TTL_MS: %w", err)
		}
		cfg.ApprovalTTLMS = ms
	}
	if cfg.ApprovalTTLMS <= 0 {
		return cfg, fmt.Errorf("approval_ttl_ms must be positive")
	}
	return cfg, nil
}

// ApprovalTTL converts the millisecond setting.
func (c Config) ApprovalTTL() time.Duration { return time.Duration(c.ApprovalTTLMS) * time.Millisecond }

// SweepInterval converts the millisecond setting.
func (c Config) SweepInterval() time.Duration {
	if c.SweepEveryMS <= 0 {
		return 5 * time.Second
	}
	return time.Duration(c.SweepEveryMS) * time.Millisecond
}
