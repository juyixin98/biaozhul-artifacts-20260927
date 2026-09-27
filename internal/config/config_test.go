package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDefaultsValid(t *testing.T) {
	if err := Default().Validate(); err != nil {
		t.Fatalf("default config invalid: %v", err)
	}
}

func TestRejectBadPolicy(t *testing.T) {
	cfg := Default()
	cfg.Reassembly.OverlapPolicy = "nope"
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected validation error for bad overlap policy")
	}
}

func TestEnvOverride(t *testing.T) {
	t.Setenv("TCPREASM_OVERLAP_POLICY", "quarantine")
	t.Setenv("TCPREASM_HTTP_ADDR", "127.0.0.1:9999")
	cfg, err := Load("")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Reassembly.OverlapPolicy != PolicyQuarantine {
		t.Fatalf("policy override = %q", cfg.Reassembly.OverlapPolicy)
	}
	if cfg.HTTP.Addr != "127.0.0.1:9999" {
		t.Fatalf("addr override = %q", cfg.HTTP.Addr)
	}
}

func TestFileThenEnv(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "cfg.json")
	if err := os.WriteFile(path, []byte(`{
	  "http": {"addr": "127.0.0.1:1111"},
	  "reassembly": {"overlap_policy": "last-wins", "max_buffered_bytes_per_dir": 1024}
	}`), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TCPREASM_OVERLAP_POLICY", "first-wins")
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTP.Addr != "127.0.0.1:1111" {
		t.Fatalf("file value not loaded: %q", cfg.HTTP.Addr)
	}
	if cfg.Reassembly.OverlapPolicy != PolicyFirstWins {
		t.Fatalf("env must override file: %q", cfg.Reassembly.OverlapPolicy)
	}
	if cfg.Reassembly.MaxBufferedBytesPerDir != 1024 {
		t.Fatal("remaining file values must survive")
	}
}

func TestPreviewBounds(t *testing.T) {
	cfg := Default()
	cfg.Diagnostics.PayloadPreviewBytes = 300
	if err := cfg.Validate(); err == nil {
		t.Fatal("preview > 256 must fail")
	}
}
