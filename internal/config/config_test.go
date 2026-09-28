package config_test

import (
	"os"
	"path/filepath"
	"testing"

	"placer/internal/config"
)

func TestLoad_DefaultsWhenMissing(t *testing.T) {
	cfg, err := config.Load(filepath.Join(t.TempDir(), "absent.json"))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTPAddr != ":8080" {
		t.Fatalf("default addr wrong: %s", cfg.HTTPAddr)
	}
	if cfg.DefaultPlacement.SpreadTopologyKey != "zone" {
		t.Fatalf("default spread key wrong: %s", cfg.DefaultPlacement.SpreadTopologyKey)
	}
	if cfg.DefaultPlacement.SkewDomainMode != "configured" {
		t.Fatalf("default skew mode wrong: %s", cfg.DefaultPlacement.SkewDomainMode)
	}
}

func TestLoad_FileAndEnvOverride(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "c.json")
	body := `{"http_addr":":9999","database":"test.db","log_level":"error",
	"default_placement":{"spread_topology_key":"region","skew_domain_mode":"eligible",
	"include_empty_domains":false,"max_pending_for_exact":5,"search_budget":1000}}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PLACER_HTTP_ADDR", ":7777")
	cfg, err := config.Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTPAddr != ":7777" {
		t.Fatalf("env override must win, got %s", cfg.HTTPAddr)
	}
	if cfg.Database != "test.db" || cfg.LogLevel != "error" {
		t.Fatalf("file values wrong: %+v", cfg)
	}
	if cfg.DefaultPlacement.SpreadTopologyKey != "region" ||
		cfg.DefaultPlacement.SkewDomainMode != "eligible" ||
		cfg.DefaultPlacement.MaxPendingForExact != 5 {
		t.Fatalf("placement defaults wrong: %+v", cfg.DefaultPlacement)
	}
}

func TestLoad_RejectsInvalidValues(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(path, []byte(`{"log_level":"verbose"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := config.Load(path); err == nil {
		t.Fatal("invalid log level must be rejected")
	}
}
