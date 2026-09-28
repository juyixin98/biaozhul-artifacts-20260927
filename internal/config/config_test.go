package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDefaultIsValid(t *testing.T) {
	if err := Default().Validate(); err != nil {
		t.Fatalf("defaults invalid: %v", err)
	}
}

func TestLoadFileAndEnvOverride(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "cfg.json")
	if err := os.WriteFile(path, []byte(`{
		"http": {"listen": "127.0.0.1:9099"},
		"db_path": "/tmp/should-be-overridden.db",
		"overlap_policy": "first_wins",
		"max_packets_per_request": 5,
		"max_upload_bytes": 1024
	}`), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TCPREPLAY_DB", "/tmp/from-env.db")
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTP.Listen != "127.0.0.1:9099" {
		t.Errorf("listen: %s", cfg.HTTP.Listen)
	}
	if cfg.DBPath != "/tmp/from-env.db" {
		t.Errorf("env override failed: %s", cfg.DBPath)
	}
	if cfg.MaxPacketsPerRequest != 5 {
		t.Errorf("packets limit: %d", cfg.MaxPacketsPerRequest)
	}
}

func TestInvalidPolicyRejected(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(path, []byte(`{"overlap_policy": "random"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := Load(path)
	if err == nil || !strings.Contains(err.Error(), "overlap_policy") {
		t.Fatalf("want overlap_policy validation error, got %v", err)
	}
}

func TestEmptyPathUsesDefaults(t *testing.T) {
	cfg, err := Load("")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.OverlapPolicy != "quarantine" {
		t.Errorf("default policy should be safest quarantine, got %s", cfg.OverlapPolicy)
	}
}
