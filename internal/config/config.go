// Package config loads all runtime configuration from environment variables with
// fixed defaults. Configuration is independent of the other modules: nothing in
// the protocol/kernel/store packages reads the environment directly.
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is the validated process configuration.
type Config struct {
	HTTPAddr     string
	DatabaseURL  string
	MaxConns     int32
	MaxBodyBytes int64

	// RequestTimeout bounds a single HTTP request's work, including DB calls.
	RequestTimeout time.Duration

	// RedactTopics replaces the literal levels in diagnostics with their length
	// when true (levels that are wildcards are always shown). Payloads are
	// ALWAYS replaced by a SHA-256 prefix, regardless of this flag.
	RedactTopics bool
}

const (
	defaultAddr         = "127.0.0.1:8080"
	defaultMaxConns     = 10
	defaultMaxBodyBytes = 1 << 20 // 1 MiB
	defaultTimeout      = 10 * time.Second
)

// Load reads configuration from the environment. Missing values take defaults.
func Load() (Config, error) {
	c := Config{
		HTTPAddr:       getenv("ROUTER_HTTP_ADDR", defaultAddr),
		DatabaseURL:    getenv("ROUTER_DATABASE_URL", "postgres://router:router_dev_pwd@localhost:5432/topicrouter?sslmode=disable"),
		MaxConns:       defaultMaxConns,
		MaxBodyBytes:   defaultMaxBodyBytes,
		RequestTimeout: defaultTimeout,
	}

	if v := os.Getenv("ROUTER_DB_MAX_CONNS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > 1000 {
			return Config{}, fmt.Errorf("ROUTER_DB_MAX_CONNS must be an integer in [1,1000], got %q", v)
		}
		c.MaxConns = int32(n)
	}
	if v := os.Getenv("ROUTER_MAX_BODY_BYTES"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil || n < 1024 {
			return Config{}, fmt.Errorf("ROUTER_MAX_BODY_BYTES must be an integer >= 1024, got %q", v)
		}
		c.MaxBodyBytes = n
	}
	if v := os.Getenv("ROUTER_REQUEST_TIMEOUT"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			return Config{}, fmt.Errorf("ROUTER_REQUEST_TIMEOUT must be a positive duration (e.g. 10s), got %q", v)
		}
		c.RequestTimeout = d
	}
	if v := os.Getenv("ROUTER_REDACT_TOPICS"); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("ROUTER_REDACT_TOPICS must be a boolean, got %q", v)
		}
		c.RedactTopics = b
	}

	if err := c.validate(); err != nil {
		return Config{}, err
	}
	return c, nil
}

func (c Config) validate() error {
	var problems []string
	if c.HTTPAddr == "" {
		problems = append(problems, "http addr is empty")
	}
	if !strings.Contains(c.DatabaseURL, "://") {
		problems = append(problems, "database url must be a postgres:// URL")
	}
	if c.MaxConns < 1 {
		problems = append(problems, "max conns < 1")
	}
	if c.RequestTimeout <= 0 {
		problems = append(problems, "request timeout <= 0")
	}
	if len(problems) > 0 {
		return errors.New("invalid config: " + strings.Join(problems, "; "))
	}
	return nil
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// Summary returns a log-safe one-line summary (credentials stripped).
func (c Config) Summary() string {
	safeURL := stripCredentials(c.DatabaseURL)
	return fmt.Sprintf("addr=%s db=%s max_conns=%d max_body_bytes=%d request_timeout=%s redact_topics=%t",
		c.HTTPAddr, safeURL, c.MaxConns, c.MaxBodyBytes, c.RequestTimeout, c.RedactTopics)
}

func stripCredentials(u string) string {
	at := strings.Index(u, "@")
	if at < 0 {
		return u
	}
	scheme := strings.Index(u, "://")
	if scheme < 0 || at < scheme {
		return u
	}
	return u[:scheme+3] + "***@" + u[at+1:]
}
