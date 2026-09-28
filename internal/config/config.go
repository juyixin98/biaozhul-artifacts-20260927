// Package config loads standalone service configuration from a JSON file and
// environment overrides. It deliberately uses only the standard library, so
// configuration has no hidden dependency on the rest of the service.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
)

// Config is the complete service configuration.
type Config struct {
	HTTPAddr string `json:"http_addr"`
	Store    string `json:"store"` // "memory" or "postgres"
	LogLevel string `json:"log_level"`
	Postgres PostgresConfig `json:"postgres"`
}

// PostgresConfig configures the PostgreSQL store. DSN is a lib/pq connection
// string, e.g. "host=127.0.0.1 port=5432 user=topicrouter password=... dbname=topicrouter sslmode=disable".
type PostgresConfig struct {
	DSN                string `json:"dsn"`
	MaxOpenConns       int    `json:"max_open_conns"`
	MaxIdleConns       int    `json:"max_idle_conns"`
	RetainVersions     int    `json:"retain_versions"` // diagnostic hint; history is always retained server-side
}

// Default returns the defaults used for local development and tests.
func Default() Config {
	return Config{
		HTTPAddr: ":8090",
		Store:    "memory",
		LogLevel: "info",
		Postgres: PostgresConfig{
			MaxOpenConns: 10,
			MaxIdleConns: 5,
			RetainVersions: 0,
		},
	}
}

// Load reads path if it exists and non-empty, applies TOPICROUTER_* env
// overrides, and validates the result.
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		buf, err := os.ReadFile(path)
		if err != nil {
			return cfg, fmt.Errorf("read config %s: %w", path, err)
		}
		if err := json.Unmarshal(buf, &cfg); err != nil {
			return cfg, fmt.Errorf("parse config %s: %w", path, err)
		}
	}
	if v := os.Getenv("TOPICROUTER_HTTP_ADDR"); v != "" {
		cfg.HTTPAddr = v
	}
	if v := os.Getenv("TOPICROUTER_STORE"); v != "" {
		cfg.Store = v
	}
	if v := os.Getenv("TOPICROUTER_LOG_LEVEL"); v != "" {
		cfg.LogLevel = v
	}
	if v := os.Getenv("TOPICROUTER_PG_DSN"); v != "" {
		cfg.Postgres.DSN = v
	}
	if v := os.Getenv("TOPICROUTER_PG_MAX_OPEN"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return cfg, fmt.Errorf("TOPICROUTER_PG_MAX_OPEN: %w", err)
		}
		cfg.Postgres.MaxOpenConns = n
	}

	switch cfg.Store {
	case "memory", "postgres":
	default:
		return cfg, fmt.Errorf("store must be \"memory\" or \"postgres\", got %q", cfg.Store)
	}
	if cfg.Store == "postgres" && cfg.Postgres.DSN == "" {
		return cfg, fmt.Errorf("store=postgres requires postgres.dsn or TOPICROUTER_PG_DSN")
	}
	if cfg.HTTPAddr == "" {
		return cfg, fmt.Errorf("http_addr must not be empty")
	}
	return cfg, nil
}
