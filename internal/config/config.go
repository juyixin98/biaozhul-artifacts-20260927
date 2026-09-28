// Package config loads process configuration from environment variables with
// local-development defaults. Nothing here requires external accounts.
package config

import (
	"os"
	"strconv"
	"time"
)

// Controller is the controller process configuration.
type Controller struct {
	HTTPAddr       string
	DatabaseDSN    string
	ExternalURL    string
	Workers        int
	ResyncInterval time.Duration
	BackoffBase    time.Duration
	BackoffMax     time.Duration
}

// FromEnv loads controller configuration, applying defaults.
func FromEnv() Controller {
	return Controller{
		HTTPAddr:       env("RC_HTTP_ADDR", "127.0.0.1:8080"),
		DatabaseDSN:    env("RC_DB_DSN", "file:data/controller.db?cache=shared&_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)"),
		ExternalURL:    env("RC_EXTERNAL_URL", "http://127.0.0.1:8090"),
		Workers:        envInt("RC_WORKERS", 2),
		ResyncInterval: envDuration("RC_RESYNC_INTERVAL", 5*time.Second),
		BackoffBase:    envDuration("RC_BACKOFF_BASE", 200*time.Millisecond),
		BackoffMax:     envDuration("RC_BACKOFF_MAX", 10*time.Second),
	}
}

// FakeService is the actual-resource-service process configuration.
type FakeService struct {
	HTTPAddr string
}

// FakeServiceFromEnv loads the fake service configuration.
func FakeServiceFromEnv() FakeService {
	return FakeService{HTTPAddr: env("FAKE_HTTP_ADDR", "127.0.0.1:8090")}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			return d
		}
	}
	return def
}
