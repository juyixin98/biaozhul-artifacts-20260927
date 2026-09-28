// Package config loads the single local startup configuration: listen address,
// SQLite path, run-log directory, the ordered plugin chains with *explicit*
// per-plugin fail policy and timeout, and the local defaults catalog / quota
// limits. There are no remote config sources.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"admission/internal/plugins"
)

// Config is the startup file shape.
type Config struct {
	Listen       string           `json:"listen"`
	DatabasePath string           `json:"databasePath"`
	LogDir       string           `json:"logDir"`
	MaxPasses    int              `json:"maxPasses"`
	ReconcileMS  int              `json:"reconcileIntervalMs"`
	DefaultsFile string           `json:"defaultsFile"`
	QuotaLimits  map[string]int64 `json:"quotaLimits"`
	Mutators     []PluginConfig   `json:"mutators"`
	Validators   []PluginConfig   `json:"validators"`
}

// PluginConfig declares one chain position. FailPolicy must be "open" or
// "closed"; there is intentionally no default — being explicit is required.
type PluginConfig struct {
	Type      string             `json:"type"`
	Fail      plugins.FailPolicy `json:"failPolicy"`
	TimeoutMS int                `json:"timeoutMs"`
	// Type-specific knobs:
	PerReplica  int64 `json:"perReplica,omitempty"`
	MaxReplicas int64 `json:"maxReplicas,omitempty"`
}

// Timeout converts the millisecond config.
func (p PluginConfig) Timeout() time.Duration { return time.Duration(p.TimeoutMS) * time.Millisecond }

// Load reads and validates a config file.
func Load(path string) (*Config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	c.applyDefaults()
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return &c, nil
}

func (c *Config) applyDefaults() {
	if c.Listen == "" {
		c.Listen = "127.0.0.1:8080"
	}
	if c.DatabasePath == "" {
		c.DatabasePath = "data/admission.db"
	}
	if c.LogDir == "" {
		c.LogDir = "data/runs"
	}
	if c.MaxPasses == 0 {
		c.MaxPasses = 5
	}
	if c.ReconcileMS == 0 {
		c.ReconcileMS = 500
	}
	if c.DefaultsFile == "" {
		c.DefaultsFile = "configs/defaults.json"
	}
}

// Validate enforces explicit policies and known plugin types.
func (c *Config) Validate() error {
	if c.MaxPasses < 1 {
		return fmt.Errorf("maxPasses must be >= 1, got %d", c.MaxPasses)
	}
	if c.ReconcileMS < 10 {
		return fmt.Errorf("reconcileIntervalMs must be >= 10")
	}
	knownM := map[string]bool{"defaults": true, "capacity": true, "stamp-uid": true}
	knownV := map[string]bool{"schema": true, "quota": true}
	for i, p := range c.Mutators {
		if !knownM[p.Type] {
			return fmt.Errorf("mutators[%d]: unknown type %q", i, p.Type)
		}
		if !p.Fail.Valid() {
			return fmt.Errorf("mutators[%d] (%s): failPolicy must be \"open\" or \"closed\"", i, p.Type)
		}
		if p.TimeoutMS < 1 {
			return fmt.Errorf("mutators[%d] (%s): timeoutMs must be >= 1", i, p.Type)
		}
	}
	for i, p := range c.Validators {
		if !knownV[p.Type] {
			return fmt.Errorf("validators[%d]: unknown type %q", i, p.Type)
		}
		if !p.Fail.Valid() {
			return fmt.Errorf("validators[%d] (%s): failPolicy must be \"open\" or \"closed\"", i, p.Type)
		}
		if p.TimeoutMS < 1 {
			return fmt.Errorf("validators[%d] (%s): timeoutMs must be >= 1", i, p.Type)
		}
	}
	return nil
}

// LoadDefaultsCatalog reads the local defaults JSON.
func LoadDefaultsCatalog(path string) (plugins.DefaultsCatalog, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cat plugins.DefaultsCatalog
	if err := json.Unmarshal(b, &cat); err != nil {
		return nil, fmt.Errorf("parse defaults catalog: %w", err)
	}
	return cat, nil
}
