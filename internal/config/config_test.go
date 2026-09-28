package config_test

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"ipreasm/internal/config"
)

func TestLoadSample(t *testing.T) {
	cfg, err := config.Load(filepath.Join("..", "..", "config", "reasm.json"))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Timeout.Duration != 30*time.Second || cfg.MaxDatagramSize != 65535 {
		t.Fatalf("unexpected values: %+v", cfg)
	}
	t.Logf("loaded sample config: timeout=%s max_datagram_size=%d db=%s listen=%s",
		cfg.Timeout.Duration, cfg.MaxDatagramSize, cfg.DBPath, cfg.Listen)
}

func TestDefaultsValid(t *testing.T) {
	if err := config.Default().Validate(); err != nil {
		t.Fatalf("defaults invalid: %v", err)
	}
}

func TestInvalidConfig(t *testing.T) {
	bad := []string{
		`{"timeout":"0s"}`,
		`{"max_datagram_size":70000}`,
		`{"max_datagrams":0}`,
		`{"max_buffered_bytes":-1}`,
		`{"db_path":""}`,
		`{"unknown_field":1}`,
		`{"timeout":"nonsense"}`,
	}
	for i, body := range bad {
		p := filepath.Join(t.TempDir(), "bad.json")
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := config.Load(p); err == nil {
			t.Fatalf("case %d (%s) accepted, want error", i, body)
		}
	}
	t.Logf("all %d malformed configs rejected explicitly", len(bad))
}
