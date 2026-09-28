// Package config loads and validates the standalone configuration layer.
// Values come exclusively from LIFECYCLE_* environment variables so the
// service needs no production accounts; a file-less fixture run uses
// the defaults.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"lifecycle.local/v1/internal/model"
)

// Config is the validated runtime configuration.
type Config struct {
	// DSN for modernc.org/sqlite. Defaults to a local file under CWD;
	// use "file:test?mode=memory&_pragma=foreign_keys(1)" style DSNs in
	// tests.
	DSN string
	// HTTPListen is the bind address of the HTTP API.
	HTTPListen string
	// TickInterval bounds the background reconciler. A value of 0 runs
	// the loop as fast as possible; tests normally drive ticks via
	// POST /internal/tick instead.
	TickInterval time.Duration
	// RunID is stamped into every log line and every GC event so test
	// logs can be correlated with a run identity.
	RunID string

	// Deterministic switches the clock and UID generator to seeded,
	// reproducible implementations (fixture mode).
	Deterministic bool
	// DeterministicSeed seeds the deterministic UID generator.
	DeterministicSeed uint64
	// ClockStart is the fixed start instant of the step clock.
	ClockStart time.Time

	// UserFinalizers lists user finalizer keys that may be attached to
	// resources. Each needs a handler registered in the finalizer
	// registry; startup fails if a listed key is reserved or unknown.
	UserFinalizers []string
}

// DefaultDSN is the local SQLite file used when no DSN is configured.
const DefaultDSN = "file:lifecycle.db?_pragma=busy_timeout(5000)&_pragma=foreign_keys(ON)&_pragma=journal_mode(WAL)"

// DefaultConfig returns the local defaults.
func DefaultConfig() Config {
	return Config{
		DSN:          DefaultDSN,
		HTTPListen:   "127.0.0.1:18080",
		TickInterval: 500 * time.Millisecond,
		RunID:        "",
		UserFinalizers: []string{
			"fixture.local/audit",
			"fixture.local/flaky",
			"fixture.local/fail",
			"fixture.local/panic",
		},
	}
}

// Load reads configuration from the environment and validates it.
func Load() (Config, error) {
	cfg := DefaultConfig()
	if v := strings.TrimSpace(os.Getenv("LIFECYCLE_DSN")); v != "" {
		cfg.DSN = v
	}
	if v := strings.TrimSpace(os.Getenv("LIFECYCLE_HTTP_LISTEN")); v != "" {
		cfg.HTTPListen = v
	}
	if v := strings.TrimSpace(os.Getenv("LIFECYCLE_TICK_INTERVAL")); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return cfg, fmt.Errorf("LIFECYCLE_TICK_INTERVAL invalid: %w", err)
		}
		cfg.TickInterval = d
	}
	if v := strings.TrimSpace(os.Getenv("LIFECYCLE_RUN_ID")); v != "" {
		cfg.RunID = v
	}
	if v := strings.TrimSpace(os.Getenv("LIFECYCLE_DETERMINISTIC")); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return cfg, fmt.Errorf("LIFECYCLE_DETERMINISTIC invalid: %w", err)
		}
		cfg.Deterministic = b
	}
	if v := strings.TrimSpace(os.Getenv("LIFECYCLE_SEED")); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			return cfg, fmt.Errorf("LIFECYCLE_SEED invalid: %w", err)
		}
		cfg.DeterministicSeed = n
	}
	if v := strings.TrimSpace(os.Getenv("LIFECYCLE_CLOCK_START")); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			return cfg, fmt.Errorf("LIFECYCLE_CLOCK_START invalid (want RFC3339): %w", err)
		}
		cfg.ClockStart = t
	}
	if v := strings.TrimSpace(os.Getenv("LIFECYCLE_USER_FINALIZERS")); v != "" {
		cfg.UserFinalizers = splitCSV(v)
	}
	return cfg, cfg.Validate()
}

// Validate rejects unusable configuration early.
func (c Config) Validate() error {
	if strings.TrimSpace(c.DSN) == "" {
		return fmt.Errorf("dsn must not be empty")
	}
	if strings.TrimSpace(c.HTTPListen) == "" {
		return fmt.Errorf("httpListen must not be empty")
	}
	if c.TickInterval < 0 {
		return fmt.Errorf("tickInterval must be >= 0")
	}
	seen := map[string]bool{}
	for _, f := range c.UserFinalizers {
		if strings.TrimSpace(f) == "" {
			return fmt.Errorf("userFinalizers contains an empty key")
		}
		if strings.HasPrefix(f, model.ReservedPrefix) {
			return fmt.Errorf("user finalizer %q uses reserved prefix %q", f, model.ReservedPrefix)
		}
		if seen[f] {
			return fmt.Errorf("user finalizer %q listed twice", f)
		}
		seen[f] = true
	}
	if c.Deterministic && c.DeterministicSeed == 0 {
		// 0 would be silently turned into 1 inside the generator; make
		// the choice explicit at the config boundary instead.
		return fmt.Errorf("deterministic mode requires a non-zero LIFECYCLE_SEED")
	}
	return nil
}

func splitCSV(v string) []string {
	parts := strings.Split(v, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
