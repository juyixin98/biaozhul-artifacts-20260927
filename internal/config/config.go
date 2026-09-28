// Package config provides standalone configuration for the placement service:
// a typed JSON file plus defaults and validation. It deliberately has no
// dependency on scheduler/store packages so configuration can be tested and
// shipped independently.
package config

import (
	"encoding/json"
	"fmt"
	"os"
)

// Config is the root configuration object.
type Config struct {
	HTTP      HTTPConfig      `json:"http"`
	Storage   StorageConfig   `json:"storage"`
	Scheduler SchedulerConfig `json:"scheduler"`
	Reconcile ReconcileConfig `json:"reconcile"`
	Logging   LoggingConfig   `json:"logging"`
}

// HTTPConfig configures the net/http server.
type HTTPConfig struct {
	Addr            string `json:"addr"`
	ShutdownTimeout string `json:"shutdown_timeout"`
}

// StorageConfig configures the SQLite adapter.
type StorageConfig struct {
	// DSN e.g. "file:placement.db?cache=shared&_pragma=busy_timeout(5000)".
	DSN string `json:"dsn"`
	// Reset wipes and re-creates schema on startup (test/demo convenience).
	Reset bool `json:"reset"`
}

// SchedulerConfig tunes search and soft objectives.
type SchedulerConfig struct {
	// ExhaustiveMaxInstances / ExhaustiveMaxCandidates cap exhaustive search;
	// when a request exceeds either cap the solver falls back to deterministic
	// most-constrained-first greedy search and records this in the trace.
	ExhaustiveMaxInstances  int `json:"exhaustive_max_instances"`
	ExhaustiveMaxCandidates int `json:"exhaustive_max_candidates"`
	// MaxLeafVisits bounds the total number of complete assignments explored;
	// 0 means "unbounded" (subject to the caps above).
	MaxLeafVisits int `json:"max_leaf_visits"`

	Skew SkewConfig `json:"skew"`
}

// SkewConfig defines how regional-distribution skew is measured.
//
// The set D* of domains participating in skew computation is:
//
//   - always: every zone that contains at least one ELIGIBLE node at request
//     time;
//   - additionally, when IncludeDeclaredEmpty is true, zones listed in
//     DeclaredZones that contain zero nodes at all (genuinely empty domains);
//   - zones whose nodes are ALL ineligible are never participation domains
//     (their load is undefined — there is nowhere to place). If a pinned
//     instance targets such a zone this is a hard DOMAIN_NO_ELIGIBLE_NODE
//     failure, not a skew consideration.
//
// Ineligible nodes themselves never contribute to zone load: load counts
// placed/allocated instances only.
type SkewConfig struct {
	// IncludeDeclaredEmpty pulls truly empty declared zones into D*.
	IncludeDeclaredEmpty bool     `json:"include_declared_empty"`
	DeclaredZones        []string `json:"declared_zones,omitempty"`
}

// ReconcileConfig tunes the reconciliation loop.
type ReconcileConfig struct {
	// Interval between periodic reconcile ticks; "0s" disables the periodic
	// loop (POST /reconcile still works).
	Interval string `json:"interval"`
	// AllowRecreate permits evicting a running instance before its replacement
	// is placed. When false (default, safer rolling semantics) a replacement
	// that cannot reserve capacity on a new node first fails with
	// ROLLING_REPLACEMENT_BLOCKED instead of producing downtime.
	AllowRecreate bool `json:"allow_recreate"`
	// MaxPlansPerTick bounds how many plans one tick emits.
	MaxPlansPerTick int `json:"max_plans_per_tick"`
}

// LoggingConfig configures structured logs.
type LoggingConfig struct {
	// Level: debug|info|warn|error.
	Level  string `json:"level"`
	// Human renders logfmt instead of JSON.
	Human  bool   `json:"human"`
}

// Default returns the built-in default configuration.
func Default() Config {
	return Config{
		HTTP: HTTPConfig{Addr: ":8080", ShutdownTimeout: "10s"},
		Storage: StorageConfig{
			DSN: "file:placement.db?cache=shared&_pragma=busy_timeout(5000)",
		},
		Scheduler: SchedulerConfig{
			ExhaustiveMaxInstances:  8,
			ExhaustiveMaxCandidates: 12,
			MaxLeafVisits:           200000,
			Skew: SkewConfig{
				IncludeDeclaredEmpty: false,
			},
		},
		Reconcile: ReconcileConfig{
			Interval:        "30s",
			AllowRecreate:   false,
			MaxPlansPerTick: 10,
		},
		Logging: LoggingConfig{Level: "info", Human: false},
	}
}

// Load reads a JSON config file. An empty path returns Default().
func Load(path string) (Config, error) {
	cfg := Default()
	if path == "" {
		return cfg, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config %s: %w", path, err)
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, fmt.Errorf("parse config %s: %w", path, err)
	}
	if err := cfg.Validate(); err != nil {
		return cfg, err
	}
	return cfg, nil
}

// Validate checks cross-field invariants.
func (c Config) Validate() error {
	if c.HTTP.Addr == "" {
		return fmt.Errorf("http.addr must not be empty")
	}
	if c.Storage.DSN == "" {
		return fmt.Errorf("storage.dsn must not be empty")
	}
	if c.Scheduler.ExhaustiveMaxInstances < 0 {
		return fmt.Errorf("scheduler.exhaustive_max_instances must be >= 0")
	}
	if c.Scheduler.ExhaustiveMaxCandidates < 0 {
		return fmt.Errorf("scheduler.exhaustive_max_candidates must be >= 0")
	}
	switch c.Logging.Level {
	case "", "debug", "info", "warn", "error":
	default:
		return fmt.Errorf("logging.level %q invalid (debug|info|warn|error)", c.Logging.Level)
	}
	return nil
}
