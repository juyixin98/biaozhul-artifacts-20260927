package config

import (
	"strings"
	"testing"
)

func TestLoad_Defaults(t *testing.T) {
	t.Setenv("ROUTER_DATABASE_URL", "")
	t.Setenv("ROUTER_HTTP_ADDR", "")
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.MaxConns != 10 || c.MaxBodyBytes != 1<<20 || c.RequestTimeout.String() != "10s" {
		t.Fatalf("defaults wrong: %+v", c)
	}
	if !strings.Contains(c.DatabaseURL, "postgres://") {
		t.Fatalf("default db url wrong: %s", c.DatabaseURL)
	}
}

func TestLoad_OverridesAndValidation(t *testing.T) {
	t.Setenv("ROUTER_HTTP_ADDR", "0.0.0.0:9999")
	t.Setenv("ROUTER_DB_MAX_CONNS", "23")
	t.Setenv("ROUTER_MAX_BODY_BYTES", "4096")
	t.Setenv("ROUTER_REQUEST_TIMEOUT", "250ms")
	t.Setenv("ROUTER_REDACT_TOPICS", "true")
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.HTTPAddr != "0.0.0.0:9999" || c.MaxConns != 23 ||
		c.MaxBodyBytes != 4096 || c.RequestTimeout.String() != "250ms" || !c.RedactTopics {
		t.Fatalf("override values wrong: %+v", c)
	}

	for name, env := range map[string]string{
		"ROUTER_DB_MAX_CONNS":    "0",
		"ROUTER_MAX_BODY_BYTES":  "abc",
		"ROUTER_REQUEST_TIMEOUT": "-1s",
		"ROUTER_REDACT_TOPICS":   "maybe",
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(name, env)
			if _, err := Load(); err == nil {
				t.Fatalf("expected error for %s=%s", name, env)
			}
		})
	}
}

func TestSummary_StripsCredentials(t *testing.T) {
	c := Config{
		HTTPAddr: "x", DatabaseURL: "postgres://user:supersecret@host:5432/db",
		MaxConns: 1, MaxBodyBytes: 2048, RequestTimeout: 1,
	}
	out := c.Summary()
	if strings.Contains(out, "supersecret") {
		t.Fatalf("summary leaked password: %s", out)
	}
	if !strings.Contains(out, "***@host") {
		t.Fatalf("summary did not mask userinfo: %s", out)
	}
}
