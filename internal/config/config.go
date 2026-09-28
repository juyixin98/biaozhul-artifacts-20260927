// Package config parses and validates the service configuration: one JSON
// file, with TCPREPLAY_* environment variables overriding individual keys.
// Config files are the only configuration mechanism; there are no hidden
// defaults beyond documented zero values.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"

	"tcpreplay/internal/reassembly"
)

// Config is the full application configuration.
type Config struct {
	HTTP HTTPConfig `json:"http"`
	// DBPath is the SQLite file. Use ":memory:" for ephemeral tests.
	DBPath string `json:"db_path"`
	// OverlapPolicy is first_wins | last_wins | quarantine.
	OverlapPolicy string `json:"overlap_policy"`
	// PayloadPreview controls whether diagnostic events may include a short
	// hex preview of segment payloads. Default false: payloads are never
	// logged, only counted and stored as stream evidence.
	PayloadPreview bool `json:"payload_preview"`
	// MaxPacketPerRequest bounds one ingest to keep memory predictable.
	MaxPacketsPerRequest int `json:"max_packets_per_request"`
	// MaxUploadBytes bounds a pcap upload body.
	MaxUploadBytes int64 `json:"max_upload_bytes"`
}

// HTTPConfig configures the HTTP listener.
type HTTPConfig struct {
	// Listen defaults to 127.0.0.1:8080; bind 0.0.0.0 deliberately.
	Listen string `json:"listen"`
}

// Default returns the built-in defaults.
func Default() Config {
	return Config{
		HTTP:                 HTTPConfig{Listen: "127.0.0.1:8080"},
		DBPath:               "data/tcpreplay.db",
		OverlapPolicy:        string(reassembly.PolicyQuarantine),
		PayloadPreview:       false,
		MaxPacketsPerRequest: 200000,
		MaxUploadBytes:       64 << 20,
	}
}

// Load reads path (when non-empty), overlays environment overrides and
// validates the result. An empty path yields defaults plus overrides.
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		raw, err := os.ReadFile(path)
		if err != nil {
			return Config{}, fmt.Errorf("config: read %s: %w", path, err)
		}
		if err := json.Unmarshal(raw, &cfg); err != nil {
			return Config{}, fmt.Errorf("config: parse %s: %w", path, err)
		}
	}
	applyEnv(&cfg)
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func applyEnv(cfg *Config) {
	if v := os.Getenv("TCPREPLAY_LISTEN"); v != "" {
		cfg.HTTP.Listen = v
	}
	if v := os.Getenv("TCPREPLAY_DB"); v != "" {
		cfg.DBPath = v
	}
	if v := os.Getenv("TCPREPLAY_POLICY"); v != "" {
		cfg.OverlapPolicy = v
	}
	if v := os.Getenv("TCPREPLAY_PREVIEW"); v != "" {
		if b, err := strconv.ParseBool(v); err == nil {
			cfg.PayloadPreview = b
		}
	}
	if v := os.Getenv("TCPREPLAY_MAX_PACKETS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.MaxPacketsPerRequest = n
		}
	}
	if v := os.Getenv("TCPREPLAY_MAX_UPLOAD"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			cfg.MaxUploadBytes = n
		}
	}
}

// Validate checks every field.
func (c Config) Validate() error {
	var problems []string
	if c.HTTP.Listen == "" {
		problems = append(problems, "http.listen must not be empty")
	}
	if c.DBPath == "" {
		problems = append(problems, "db_path must not be empty")
	}
	if !reassembly.ValidPolicy(reassembly.OverlapPolicy(c.OverlapPolicy)) {
		problems = append(problems, fmt.Sprintf("overlap_policy must be one of first_wins|last_wins|quarantine, got %q", c.OverlapPolicy))
	}
	if c.MaxPacketsPerRequest <= 0 {
		problems = append(problems, "max_packets_per_request must be positive")
	}
	if c.MaxUploadBytes <= 0 {
		problems = append(problems, "max_upload_bytes must be positive")
	}
	if len(problems) > 0 {
		return fmt.Errorf("config invalid: %s", strings.Join(problems, "; "))
	}
	return nil
}
