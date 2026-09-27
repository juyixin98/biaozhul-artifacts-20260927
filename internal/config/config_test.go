package config_test

import (
	"os"
	"path/filepath"
	"testing"

	"flowrouter/internal/apperr"
	"flowrouter/internal/config"
)

func writeTemp(t *testing.T, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestLoadValid(t *testing.T) {
	cfg, err := config.Load(validConfigPath(t))
	if err != nil {
		t.Fatalf("load example config: %v", err)
	}
	if cfg.VNodesPerWeight != 160 {
		t.Errorf("vnodes_per_weight=%d", cfg.VNodesPerWeight)
	}
	if len(cfg.Members) != 3 {
		t.Fatalf("members=%d", len(cfg.Members))
	}
	if cfg.Members[2].Weight != 2 {
		t.Errorf("hop-c weight=%d", cfg.Members[2].Weight)
	}
}

func validConfigPath(t *testing.T) string {
	t.Helper()
	// locate repo-root configs/config.example.yaml from this test
	cwd, _ := os.Getwd()
	return filepath.Join(cwd, "..", "..", "configs", "config.example.yaml")
}

func TestLoadZeroWeightsAccepted(t *testing.T) {
	p := writeTemp(t, `
listen: "127.0.0.1:0"
vnodes_per_weight: 4
store:
  path: ":memory:"
members:
  - id: a
    weight: 0
  - id: b
    weight: 0
`)
	cfg, err := config.Load(p)
	if err != nil {
		t.Fatalf("all-zero weights must be accepted at config layer: %v", err)
	}
	if cfg.Members[1].ID != "b" {
		t.Fatal("member parse")
	}
}

func TestInvalidConfigs(t *testing.T) {
	cases := []struct {
		name string
		body string
		code string
	}{
		{"empty_listen", "vnodes_per_weight: 4\nlisten: \"\"\nstore:\n  path: \":memory:\"\n", "EMPTY_LISTEN"},
		{"bad_vpw", "vnodes_per_weight: 0\nstore:\n  path: \":memory:\"\n", "BAD_VNODES_PER_WEIGHT"},
		{"duplicate_id", "members:\n- id: a\n  weight: 1\n- id: a\n  weight: 1\n", "MEMBER_DUPLICATE_ID"},
		{"empty_id", "members:\n- id: \"\"\n  weight: 1\n", "MEMBER_EMPTY_ID"},
		{"negative_weight", "members:\n- id: a\n  weight: -2\n", "MEMBER_NEGATIVE_WEIGHT"},
		{"bad_address", "members:\n- id: a\n  weight: 1\n  address: \"not-an-address\"\n", "MEMBER_BAD_ADDRESS"},
		{"cap_too_small", "vnodes_per_weight: 4\nmax_vnodes: 1\nmembers:\n- id: a\n  weight: 1\n- id: b\n  weight: 1\n", "CAP_TOO_SMALL"},
		{"bad_duration", "health:\n  enabled: true\n  interval: \"not-a-duration\"\n", "BAD_DURATION"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := writeTemp(t, tc.body)
			_, err := config.Load(p)
			ae, ok := apperr.As(err)
			if !ok {
				t.Fatalf("err=%v, want structured apperr", err)
			}
			if ae.Kind != apperr.KindInvalidInput {
				t.Errorf("kind=%s, want INVALID_INPUT", ae.Kind)
			}
			if ae.Code != tc.code {
				t.Errorf("code=%s, want %s (msg=%s)", ae.Code, tc.code, ae.Message)
			}
		})
	}
}

func TestMissingAndMalformedFile(t *testing.T) {
	if _, err := config.Load(filepath.Join(t.TempDir(), "nope.yaml")); err == nil {
		t.Fatal("missing file must error")
	} else if ae, ok := apperr.As(err); !ok || ae.Code != "CONFIG_UNREADABLE" {
		t.Fatalf("err=%v, want CONFIG_UNREADABLE", err)
	}
	p := writeTemp(t, "listen: [unterminated")
	if _, err := config.Load(p); err == nil {
		t.Fatal("malformed YAML must error")
	}
}
