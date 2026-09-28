package config_test

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"cidrsvc/internal/config"
)

func writeTemp(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestLoadDefaults(t *testing.T) {
	cfg, err := config.Load("")
	if err != nil {
		t.Fatal(err)
	}
	def := config.Default()
	if cfg.HTTPListen != def.HTTPListen || cfg.DatabasePath != def.DatabasePath ||
		cfg.MaxInputPrefixes != def.MaxInputPrefixes || cfg.ShutdownTimeout != 5*time.Second {
		t.Fatalf("defaults mismatch: %+v", cfg)
	}
}

func TestLoadFromFile(t *testing.T) {
	p := writeTemp(t, `{
		"http_listen": "0.0.0.0:9999",
		"database_path": "/tmp/x.db",
		"log_path": "-",
		"max_input_prefixes": 7,
		"shutdown_timeout": "250ms",
		"strict_cidr": true
	}`)
	cfg, err := config.Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTPListen != "0.0.0.0:9999" || cfg.MaxInputPrefixes != 7 ||
		!cfg.StrictCIDR || cfg.ShutdownTimeout != 250*time.Millisecond {
		t.Fatalf("file config not applied: %+v", cfg)
	}
}

func TestUnknownKeyRejected(t *testing.T) {
	p := writeTemp(t, `{"http_listen": ":8080", "bogus": 1}`)
	if _, err := config.Load(p); err == nil {
		t.Fatal("expected strict failure on unknown key")
	}
}

func TestInvalidValuesRejected(t *testing.T) {
	cases := map[string]string{
		"bad duration":     `{"shutdown_timeout": "not-a-duration"}`,
		"zero max":         `{"max_input_prefixes": 0}`,
		"empty listen":     `{"http_listen": ""}`,
		"empty db":         `{"database_path": ""}`,
		"negative timeout": `{"shutdown_timeout": "-1s"}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			p := writeTemp(t, body)
			if _, err := config.Load(p); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestEnvOverrides(t *testing.T) {
	p := writeTemp(t, `{"http_listen": ":8080"}`)
	t.Setenv("CIDRSVC_HTTP_LISTEN", "127.0.0.1:1234")
	t.Setenv("CIDRSVC_MAX_INPUT_PREFIXES", "42")
	t.Setenv("CIDRSVC_STRICT_CIDR", "true")
	cfg, err := config.Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTPListen != "127.0.0.1:1234" || cfg.MaxInputPrefixes != 42 || !cfg.StrictCIDR {
		t.Fatalf("env overrides not applied: %+v", cfg)
	}
}

func TestBadEnvValueRejected(t *testing.T) {
	t.Setenv("CIDRSVC_MAX_INPUT_PREFIXES", "not-a-number")
	if _, err := config.Load(""); err == nil {
		t.Fatal("expected error on bad env integer")
	}
}

func TestMissingFileReported(t *testing.T) {
	if _, err := config.Load(filepath.Join(t.TempDir(), "nope.json")); err == nil {
		t.Fatal("missing config must error, not silently default")
	}
}
