// Package config loads server configuration from a JSON file, with optional
// environment overrides. Configuration is strictly separated from test
// fixtures and code.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config is the server configuration.
type Config struct {
	HTTP HTTPConfig `json:"http"`
	// FixturePath is the synthetic desired-state file the reconciler polls.
	FixturePath string `json:"fixturePath"`
	// ReconcileInterval is the desired-state poll interval; "0s" means
	// reconcile only at startup and on POST /internal/refresh.
	ReconcileInterval Duration     `json:"reconcileInterval"`
	SQLite            SQLiteConfig `json:"sqlite"`
	HistoryKeep       int          `json:"historyKeep"`
	LogLevel          string       `json:"logLevel"`
}

// HTTPConfig configures the listener.
type HTTPConfig struct {
	Addr string `json:"addr"`
}

// SQLiteConfig configures the persistence layer.
type SQLiteConfig struct {
	// DSN is a filesystem path or SQLite DSN, e.g.
	// "file:data/netpolicy.db?cache=shared".
	DSN string `json:"dsn"`
}

// Duration is a time.Duration that (un)marshals as e.g. "5s".
type Duration struct{ time.Duration }

// MarshalJSON renders the duration as its string form.
func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.Duration.String())
}

// UnmarshalJSON accepts a duration string.
func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	d.Duration = v
	return nil
}

// Load reads path and applies environment overrides:
//
//	NETPOL_FIXTURE_PATH, NETPOL_SQLITE_DSN, NETPOL_HTTP_ADDR,
//	NETPOL_RECONCILE_INTERVAL, NETPOL_LOG_LEVEL, NETPOL_HISTORY_KEEP.
func Load(path string) (*Config, error) {
	cfg := defaults()
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config %s: %w", path, err)
	}
	if err := json.Unmarshal(raw, cfg); err != nil {
		return nil, fmt.Errorf("parse config %s: %w", path, err)
	}
	cfg.applyEnv()
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

func defaults() *Config {
	return &Config{
		HTTP:              HTTPConfig{Addr: ":8080"},
		ReconcileInterval: Duration{5 * time.Second},
		SQLite:            SQLiteConfig{DSN: "file:data/netpolicy.db?cache=shared"},
		HistoryKeep:       10,
		LogLevel:          "info",
	}
}

func (c *Config) applyEnv() {
	if v := os.Getenv("NETPOL_FIXTURE_PATH"); v != "" {
		c.FixturePath = v
	}
	if v := os.Getenv("NETPOL_SQLITE_DSN"); v != "" {
		c.SQLite.DSN = v
	}
	if v := os.Getenv("NETPOL_HTTP_ADDR"); v != "" {
		c.HTTP.Addr = v
	}
	if v := os.Getenv("NETPOL_RECONCILE_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			c.ReconcileInterval.Duration = d
		}
	}
	if v := os.Getenv("NETPOL_LOG_LEVEL"); v != "" {
		c.LogLevel = v
	}
	if v := os.Getenv("NETPOL_HISTORY_KEEP"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			c.HistoryKeep = n
		}
	}
}

// Validate checks configuration invariants.
func (c *Config) Validate() error {
	if c.FixturePath == "" {
		return fmt.Errorf("fixturePath must be set")
	}
	if c.SQLite.DSN == "" {
		return fmt.Errorf("sqlite.dsn must be set")
	}
	if c.HTTP.Addr == "" {
		return fmt.Errorf("http.addr must be set")
	}
	if c.ReconcileInterval.Duration < 0 {
		return fmt.Errorf("reconcileInterval must be >= 0")
	}
	if c.HistoryKeep < 1 {
		return fmt.Errorf("historyKeep must be >= 1")
	}
	return nil
}
