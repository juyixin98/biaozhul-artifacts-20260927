// Package config loads standalone service configuration from a JSON file
// (default configs/placer.json) with environment overrides.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Config is the complete service configuration.
type Config struct {
	HTTPAddr string `json:"http_addr"`
	Database string `json:"database"`
	// ReconcileInterval is a duration in seconds; 0 disables the periodic
	// loop (the HTTP trigger still works).
	ReconcileIntervalSec int `json:"reconcile_interval_sec"`
	// MaxRetries before a pending instance is marked failed.
	MaxRetries int `json:"max_retries"`

	// DefaultPlacement holds defaults applied to every plan that does not
	// override them explicitly.
	DefaultPlacement PlacementDefaults `json:"default_placement"`

	LogLevel string `json:"log_level"` // debug | info | error
}

// PlacementDefaults mirrors model.PlanOptions plus solver limits.
type PlacementDefaults struct {
	SpreadTopologyKey   string `json:"spread_topology_key"`
	IncludeEmptyDomains bool   `json:"include_empty_domains"`
	SkewDomainMode      string `json:"skew_domain_mode"` // configured | eligible
	MaxPendingForExact  int    `json:"max_pending_for_exact"`
	SearchBudget        int    `json:"search_budget"`
}

// Default returns the built-in defaults.
func Default() Config {
	return Config{
		HTTPAddr:             ":8080",
		Database:             "placer.db",
		ReconcileIntervalSec: 30,
		MaxRetries:           5,
		DefaultPlacement: PlacementDefaults{
			SpreadTopologyKey:   "zone",
			IncludeEmptyDomains: true,
			SkewDomainMode:      "configured",
			MaxPendingForExact:  12,
			SearchBudget:        200000,
		},
		LogLevel: "info",
	}
}

// Load reads path if it exists and applies environment overrides. A missing
// file is not an error: defaults + overrides are returned instead.
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		data, err := os.ReadFile(path)
		switch {
		case err == nil:
			if err := json.Unmarshal(data, &cfg); err != nil {
				return Config{}, fmt.Errorf("parse %s: %w", path, err)
			}
		case errors.Is(err, os.ErrNotExist):
			// defaults stand
		default:
			return Config{}, fmt.Errorf("read %s: %w", path, err)
		}
	}
	applyEnv(&cfg)
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func applyEnv(cfg *Config) {
	if v := os.Getenv("PLACER_HTTP_ADDR"); v != "" {
		cfg.HTTPAddr = v
	}
	if v := os.Getenv("PLACER_DB"); v != "" {
		cfg.Database = v
	}
	if v := os.Getenv("PLACER_LOG_LEVEL"); v != "" {
		cfg.LogLevel = v
	}
	if v := os.Getenv("PLACER_RECONCILE_INTERVAL_SEC"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.ReconcileIntervalSec = n
		}
	}
}

// Validate enforces value ranges.
func (c Config) Validate() error {
	var problems []string
	if strings.TrimSpace(c.HTTPAddr) == "" {
		problems = append(problems, "http_addr is empty")
	}
	if strings.TrimSpace(c.Database) == "" {
		problems = append(problems, "database is empty")
	}
	if c.ReconcileIntervalSec < 0 {
		problems = append(problems, "reconcile_interval_sec must be >= 0")
	}
	if c.MaxRetries < 0 {
		problems = append(problems, "max_retries must be >= 0")
	}
	switch c.LogLevel {
	case "debug", "info", "error":
	default:
		problems = append(problems, "log_level must be debug|info|error, got "+c.LogLevel)
	}
	d := c.DefaultPlacement
	if d.SpreadTopologyKey == "" {
		problems = append(problems, "default_placement.spread_topology_key is empty")
	}
	switch d.SkewDomainMode {
	case "configured", "eligible", "":
	default:
		problems = append(problems, "skew_domain_mode must be configured|eligible, got "+d.SkewDomainMode)
	}
	if d.MaxPendingForExact < 0 || d.SearchBudget < 0 {
		problems = append(problems, "max_pending_for_exact and search_budget must be >= 0")
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}
