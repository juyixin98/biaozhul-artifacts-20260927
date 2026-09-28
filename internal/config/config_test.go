package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestLoadAppliesDefaultsFromFile(t *testing.T) {
	p := writeConfig(t, `{
		"http": {"addr": ":9090"},
		"fixturePath": "fx.json",
		"reconcileInterval": "250ms",
		"sqlite": {"dsn": "file:test.db"},
		"historyKeep": 3,
		"logLevel": "debug"
	}`)
	cfg, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTP.Addr != ":9090" || cfg.FixturePath != "fx.json" || cfg.HistoryKeep != 3 || cfg.LogLevel != "debug" {
		t.Fatalf("config not loaded: %+v", cfg)
	}
	if cfg.ReconcileInterval.Duration != 250*time.Millisecond {
		t.Fatalf("interval = %v", cfg.ReconcileInterval.Duration)
	}
}

func TestEnvOverrides(t *testing.T) {
	p := writeConfig(t, `{"fixturePath":"fx.json","sqlite":{"dsn":"file:a.db"},"http":{"addr":":1"}}`)
	t.Setenv("NETPOL_HTTP_ADDR", ":7777")
	t.Setenv("NETPOL_LOG_LEVEL", "warn")
	t.Setenv("NETPOL_RECONCILE_INTERVAL", "1s")
	cfg, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTP.Addr != ":7777" || cfg.LogLevel != "warn" || cfg.ReconcileInterval.Duration != time.Second {
		t.Fatalf("env overrides not applied: %+v", cfg)
	}
}

func TestValidationRejectsBadConfig(t *testing.T) {
	cases := map[string]string{
		"missing fixture": `{"fixturePath":"","sqlite":{"dsn":"file:a.db"},"http":{"addr":":1"}}`,
		"missing dsn":     `{"fixturePath":"fx","sqlite":{"dsn":""},"http":{"addr":":1"}}`,
		"missing addr":    `{"fixturePath":"fx","sqlite":{"dsn":"file:a.db"},"http":{"addr":""}}`,
		"negative keep":   `{"fixturePath":"fx","sqlite":{"dsn":"file:a.db"},"http":{"addr":":1"},"historyKeep":0}`,
		"bad duration":    `{"fixturePath":"fx","sqlite":{"dsn":"file:a.db"},"http":{"addr":":1"},"reconcileInterval":"soon"}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			p := writeConfig(t, body)
			if _, err := Load(p); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}
