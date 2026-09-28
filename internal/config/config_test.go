package config_test

import (
	"os"
	"path/filepath"
	"testing"

	"cidrcov/internal/config"
)

func TestDefaultIsValid(t *testing.T) {
	if err := config.Default().Validate(); err != nil {
		t.Fatalf("defaults must validate: %v", err)
	}
}

func TestLoadFromFileAndRejectsUnknownKeys(t *testing.T) {
	dir := t.TempDir()
	good := filepath.Join(dir, "good.json")
	if err := os.WriteFile(good, []byte(`{
		"listen":"127.0.0.1:9099",
		"db_path":"/tmp/x.db",
		"max_entries_per_list":42,
		"shutdown_timeout_ms":1234
	}`), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.Load(good)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Listen != "127.0.0.1:9099" || cfg.MaxEntriesPerList != 42 || cfg.ShutdownTimeoutMS != 1234 {
		t.Fatalf("values not loaded: %+v", cfg)
	}

	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte(`{"listenn":"x"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := config.Load(bad); err == nil {
		t.Fatal("unknown JSON key must be rejected")
	}
}

func TestEnvOverride(t *testing.T) {
	t.Setenv("CIDRCOV_LISTEN", "0.0.0.0:1234")
	t.Setenv("CIDRCOV_MAX_ENTRIES", "7")
	cfg, err := config.Load("")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Listen != "0.0.0.0:1234" || cfg.MaxEntriesPerList != 7 {
		t.Fatalf("env override failed: %+v", cfg)
	}
}

func TestValidateRejects(t *testing.T) {
	cfg := config.Default()
	cfg.DBPath = ""
	if err := cfg.Validate(); err == nil {
		t.Fatal("empty db_path must fail")
	}
	cfg = config.Default()
	cfg.MaxEntriesPerList = 0
	if err := cfg.Validate(); err == nil {
		t.Fatal("zero entry limit must fail")
	}
}
