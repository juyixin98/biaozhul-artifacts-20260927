// Package config holds runtime configuration loaded from the environment.
package config

import (
	"os"
	"time"
)

// Config is the coordinator process configuration.
type Config struct {
	HTTPAddr      string
	DatabaseURL   string
	SweepInterval time.Duration
}

// Load reads configuration with local-friendly defaults.
func Load() Config {
	c := Config{
		HTTPAddr:      getenv("HTTP_ADDR", ":8080"),
		DatabaseURL:   getenv("DATABASE_URL", "postgres://appuser@127.0.0.1:55601/appdb?sslmode=disable"),
		SweepInterval: 500 * time.Millisecond,
	}
	if d := getenv("SWEEP_INTERVAL", ""); d != "" {
		if parsed, err := time.ParseDuration(d); err == nil {
			c.SweepInterval = parsed
		}
	}
	return c
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
