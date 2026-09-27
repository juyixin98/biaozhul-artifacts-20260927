// Package config loads, validates and overrides service configuration.
//
// Precedence: built-in defaults < JSON config file < TCPREASM_* environment
// variables. The overlap policy is validated eagerly so a misconfiguration
// can never silently change conflict-handling semantics at runtime.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// OverlapPolicy selects how contradictory bytes at the same sequence number
// are isolated.
type OverlapPolicy string

const (
	// PolicyFirstWins keeps the bytes observed first; later contradictory
	// bytes are recorded as conflicts but discarded.
	PolicyFirstWins OverlapPolicy = "first-wins"
	// PolicyLastWins replaces buffered (not yet delivered) bytes with the
	// newcomer. Already-delivered bytes are immutable in every policy.
	PolicyLastWins OverlapPolicy = "last-wins"
	// PolicyQuarantine keeps both copies in the conflict ledger and holds
	// back delivery at the poisoned range until evidence resolves it.
	PolicyQuarantine OverlapPolicy = "quarantine"
)

// Config is the root configuration.
type Config struct {
	HTTP        HTTPConfig       `json:"http"`
	Storage     StorageConfig    `json:"storage"`
	Reassembly  ReassemblyConfig `json:"reassembly"`
	Diagnostics DiagConfig       `json:"diagnostics"`
}

// HTTPConfig configures the replay/query HTTP service.
type HTTPConfig struct {
	Addr            string `json:"addr"`
	ReadTimeoutMS   int    `json:"read_timeout_ms"`
	WriteTimeoutMS  int    `json:"write_timeout_ms"`
	MaxCaptureBytes int64  `json:"max_capture_bytes"`
}

// StorageConfig configures the SQLite state store.
type StorageConfig struct {
	// DSN is a sqlite DSN, e.g. "data/tcpreasm.db".
	DSN string `json:"dsn"`
}

// ReassemblyConfig configures core semantics.
type ReassemblyConfig struct {
	OverlapPolicy OverlapPolicy `json:"overlap_policy"`
	// MaxBufferedBytesPerDir bounds out-of-order buffer memory per
	// direction; segments beyond it are rejected (not fabricated around).
	MaxBufferedBytesPerDir int `json:"max_buffered_bytes_per_dir"`
	// DeliveredEvidenceBytes retains the tail of delivered bytes so that
	// late retransmissions overlapping delivered data can be verified.
	DeliveredEvidenceBytes int `json:"delivered_evidence_bytes"`
	// InferGenerationWithoutHandshake accepts data-only captures into an
	// inferred, marked generation instead of rejecting them outright.
	InferGenerationWithoutHandshake bool `json:"infer_generation_without_handshake"`
}

// DiagConfig configures diagnostic output and redaction.
type DiagConfig struct {
	// LogPath "" means stderr.
	LogPath string `json:"log_path"`
	// PayloadPreviewBytes is the maximum number of (masked) payload bytes
	// shown in diagnostics. Default 0: only length + SHA-256 fingerprint.
	PayloadPreviewBytes int `json:"payload_preview_bytes"`
	// MaskIPs redacts endpoint IPs in textual log lines.
	MaskIPs bool `json:"mask_ips"`
}

// Default returns the built-in defaults.
func Default() Config {
	return Config{
		HTTP: HTTPConfig{
			Addr:            "127.0.0.1:18080",
			ReadTimeoutMS:   5000,
			WriteTimeoutMS:  30000,
			MaxCaptureBytes: 16 << 20,
		},
		Storage: StorageConfig{DSN: "data/tcpreasm.db"},
		Reassembly: ReassemblyConfig{
			OverlapPolicy:                   PolicyFirstWins,
			MaxBufferedBytesPerDir:          8 << 20,
			DeliveredEvidenceBytes:          256 << 10,
			InferGenerationWithoutHandshake: true,
		},
		Diagnostics: DiagConfig{LogPath: "", PayloadPreviewBytes: 0, MaskIPs: false},
	}
}

// Load reads an optional JSON file (empty path is allowed) and applies env
// overrides. The result is validated.
func Load(path string) (Config, error) {
	cfg := Default()
	if path != "" {
		raw, err := os.ReadFile(path)
		if err != nil {
			return cfg, fmt.Errorf("read config %s: %w", path, err)
		}
		if err := json.Unmarshal(raw, &cfg); err != nil {
			return cfg, fmt.Errorf("parse config %s: %w", path, err)
		}
	}
	applyEnv(&cfg)
	if err := cfg.Validate(); err != nil {
		return cfg, err
	}
	return cfg, nil
}

// Validate checks every field that can make reassembly ambiguous.
func (c Config) Validate() error {
	switch c.Reassembly.OverlapPolicy {
	case PolicyFirstWins, PolicyLastWins, PolicyQuarantine:
	default:
		return fmt.Errorf("reassembly.overlap_policy %q invalid (want %q|%q|%q)",
			c.Reassembly.OverlapPolicy, PolicyFirstWins, PolicyLastWins, PolicyQuarantine)
	}
	if c.HTTP.Addr == "" {
		return fmt.Errorf("http.addr must not be empty")
	}
	if c.HTTP.MaxCaptureBytes <= 0 {
		return fmt.Errorf("http.max_capture_bytes must be positive")
	}
	if c.Reassembly.MaxBufferedBytesPerDir <= 0 {
		return fmt.Errorf("reassembly.max_buffered_bytes_per_dir must be positive")
	}
	if c.Reassembly.DeliveredEvidenceBytes < 0 {
		return fmt.Errorf("reassembly.delivered_evidence_bytes must be >= 0")
	}
	if c.Diagnostics.PayloadPreviewBytes < 0 || c.Diagnostics.PayloadPreviewBytes > 256 {
		return fmt.Errorf("diagnostics.payload_preview_bytes must be in [0,256]")
	}
	if c.Storage.DSN == "" {
		return fmt.Errorf("storage.dsn must not be empty")
	}
	if c.HTTP.ReadTimeoutMS < 0 || c.HTTP.WriteTimeoutMS < 0 {
		return fmt.Errorf("http timeouts must be >= 0")
	}
	return nil
}

func (h HTTPConfig) ReadTimeout() time.Duration {
	return time.Duration(h.ReadTimeoutMS) * time.Millisecond
}
func (h HTTPConfig) WriteTimeout() time.Duration {
	return time.Duration(h.WriteTimeoutMS) * time.Millisecond
}

// applyEnv maps TCPREASM_<SECTION>_<FIELD> onto config fields.
func applyEnv(c *Config) {
	if v, ok := os.LookupEnv("TCPREASM_HTTP_ADDR"); ok {
		c.HTTP.Addr = v
	}
	if v, ok := os.LookupEnv("TCPREASM_STORAGE_DSN"); ok {
		c.Storage.DSN = v
	}
	if v, ok := os.LookupEnv("TCPREASM_OVERLAP_POLICY"); ok {
		c.Reassembly.OverlapPolicy = OverlapPolicy(strings.ToLower(v))
	}
	if v, ok := os.LookupEnv("TCPREASM_MAX_BUFFERED_BYTES"); ok {
		if n, err := strconv.Atoi(v); err == nil {
			c.Reassembly.MaxBufferedBytesPerDir = n
		}
	}
	if v, ok := os.LookupEnv("TCPREASM_INFER_NO_HANDSHAKE"); ok {
		if b, err := strconv.ParseBool(v); err == nil {
			c.Reassembly.InferGenerationWithoutHandshake = b
		}
	}
	if v, ok := os.LookupEnv("TCPREASM_PAYLOAD_PREVIEW_BYTES"); ok {
		if n, err := strconv.Atoi(v); err == nil {
			c.Diagnostics.PayloadPreviewBytes = n
		}
	}
	if v, ok := os.LookupEnv("TCPREASM_DIAG_MASK_IPS"); ok {
		if b, err := strconv.ParseBool(v); err == nil {
			c.Diagnostics.MaskIPs = b
		}
	}
	if v, ok := os.LookupEnv("TCPREASM_DIAG_LOG_PATH"); ok {
		c.Diagnostics.LogPath = v
	}
}
