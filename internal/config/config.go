// Package config parses broker configuration exclusively from environment
// variables with validated ranges. Invalid values fail fast at startup rather
// than being coerced silently.
package config

import (
	"fmt"
	"os"
	"strconv"
	"time"

	"workbroker/internal/kernel"
)

// Config is the resolved process configuration.
type Config struct {
	HTTPAddr          string
	Backend           string // "memory" | "postgres"
	PostgresDSN       string
	RunID             string
	VisibilityDefault time.Duration
	VisibilityMin     time.Duration
	VisibilityMax     time.Duration
	MaxAttempts       int64
	LongPollWait      time.Duration
}

// Default returns defaults used by the server and tests.
func Default() Config {
	return Config{
		HTTPAddr:          ":8080",
		Backend:           "memory",
		PostgresDSN:       "postgres://broker:broker_local_dev_pw@127.0.0.1:5432/broker?sslmode=disable",
		RunID:             "",
		VisibilityDefault: kernel.DefaultVisibility,
		VisibilityMin:     kernel.MinVisibility,
		VisibilityMax:     kernel.MaxVisibility,
		MaxAttempts:       kernel.DefaultMaxAttempts,
		LongPollWait:      20 * time.Second,
	}
}

// FromEnv loads and validates configuration from the environment.
func FromEnv() (Config, error) {
	c := Default()
	if v := os.Getenv("BROKER_HTTP_ADDR"); v != "" {
		c.HTTPAddr = v
	}
	switch v := os.Getenv("BROKER_BACKEND"); v {
	case "", "memory":
		c.Backend = "memory"
	case "postgres":
		c.Backend = "postgres"
	default:
		return c, fmt.Errorf("BROKER_BACKEND=%q invalid: want memory|postgres", v)
	}
	if v := os.Getenv("BROKER_POSTGRES_DSN"); v != "" {
		c.PostgresDSN = v
	}
	if v := os.Getenv("BROKER_RUN_ID"); v != "" {
		c.RunID = v
	}
	if v := os.Getenv("BROKER_DEFAULT_VISIBILITY_SECONDS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return c, fmt.Errorf("BROKER_DEFAULT_VISIBILITY_SECONDS: %w", err)
		}
		d := time.Duration(n) * time.Second
		if err := validateVisibility("default", d, c.VisibilityMin, c.VisibilityMax); err != nil {
			return c, err
		}
		c.VisibilityDefault = d
	}
	if v := os.Getenv("BROKER_MAX_ATTEMPTS"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return c, fmt.Errorf("BROKER_MAX_ATTEMPTS: %w", err)
		}
		if n < 1 || n > 1000 {
			return c, fmt.Errorf("BROKER_MAX_ATTEMPTS=%d: must be within [1,1000]", n)
		}
		c.MaxAttempts = n
	}
	if v := os.Getenv("BROKER_LONGPOLL_WAIT_SECONDS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return c, fmt.Errorf("BROKER_LONGPOLL_WAIT_SECONDS: %w", err)
		}
		if n < 0 || n > 20 {
			return c, fmt.Errorf("BROKER_LONGPOLL_WAIT_SECONDS=%d: must be within [0,20]", n)
		}
		c.LongPollWait = time.Duration(n) * time.Second
	}
	return c, nil
}

func validateVisibility(label string, d, lo, hi time.Duration) error {
	if d < lo || d > hi {
		return fmt.Errorf("visibility %s=%s outside allowed range [%s,%s]", label, d, lo, hi)
	}
	return nil
}

// ValidateVisibility bounds a per-request visibility/extend duration, shared
// by the HTTP layer and tests.
func (c Config) ValidateVisibility(d time.Duration) (time.Duration, error) {
	if d < 0 {
		return 0, fmt.Errorf("negative visibility %s", d)
	}
	if d == 0 {
		return c.VisibilityDefault, nil
	}
	if err := validateVisibility("request", d, c.VisibilityMin, c.VisibilityMax); err != nil {
		return 0, err
	}
	return d, nil
}
