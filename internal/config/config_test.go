package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDefaultsValid(t *testing.T) {
	if err := Defaults().Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestLoadFileAndEnvOverride(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "cfg.json")
	if err := os.WriteFile(p, []byte(`{
		"listen_addr": "127.0.0.1:9999",
		"sqlite_dsn": "file:test.db?cache=shared",
		"max_recursion_depth": 7,
		"redact_diagnostics": false,
		"log_level": "debug"
	}`), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ListenAddr != "127.0.0.1:9999" || cfg.MaxDepth != 7 || cfg.RedactDiag {
		t.Fatalf("file values not applied: %+v", cfg)
	}

	// 环境变量覆盖。
	t.Setenv("RIB_LISTEN_ADDR", ":8181")
	t.Setenv("RIB_MAX_DEPTH", "42")
	cfg2, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if cfg2.ListenAddr != ":8181" || cfg2.MaxDepth != 42 {
		t.Fatalf("env override failed: %+v", cfg2)
	}
	// 文件内的 redact 仍生效。
	if cfg2.RedactDiag {
		t.Fatal("redact should remain false from file")
	}
}

func TestLoadEmptyPathUsesDefaults(t *testing.T) {
	t.Setenv("RIB_LISTEN_ADDR", "") // LookupEnv 对空串存在性敏感，这里取消
	os.Unsetenv("RIB_LISTEN_ADDR")
	cfg, err := Load("")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ListenAddr != "127.0.0.1:8080" {
		t.Fatalf("defaults not used: %+v", cfg)
	}
}

func TestValidateRejectsBadValues(t *testing.T) {
	cases := []Config{
		{ListenAddr: "", SQLiteDSN: "x", MaxDepth: 1, LogLevel: "info"},
		{ListenAddr: "a", SQLiteDSN: "", MaxDepth: 1, LogLevel: "info"},
		{ListenAddr: "a", SQLiteDSN: "x", MaxDepth: 0, LogLevel: "info"},
		{ListenAddr: "a", SQLiteDSN: "x", MaxDepth: 300, LogLevel: "info"},
		{ListenAddr: "a", SQLiteDSN: "x", MaxDepth: 1, LogLevel: "verbose"},
	}
	for i, c := range cases {
		err := c.Validate()
		if err == nil || !strings.Contains(err.Error(), "invalid config") {
			t.Fatalf("case %d expected validation error, got %v", i, err)
		}
	}
}

func TestBadJSON(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "bad.json")
	os.WriteFile(p, []byte(`{not json`), 0o600)
	if _, err := Load(p); err == nil {
		t.Fatal("expected parse error")
	}
}
