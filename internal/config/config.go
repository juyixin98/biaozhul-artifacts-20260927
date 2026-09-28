// Package config loads and validates the fixed-membership causal-broadcast
// configuration. Sources, in order of precedence: flags/env > optional JSON
// file > documented defaults.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
)

// Peer describes one member of the fixed group.
type Peer struct {
	ID      string `json:"id"`
	Address string `json:"address"`
}

// Config is the validated runtime configuration.
type Config struct {
	// NodeID is this process's identity; must equal one Peers[].ID.
	NodeID string `json:"node_id"`
	// ListenAddr is the HTTP listen address of this process.
	ListenAddr string `json:"listen_addr"`
	// Peers is the FULL fixed membership, including the local node. Order in
	// the file is irrelevant: canonical order is sorted by ID.
	Peers []Peer `json:"peers"`

	// BufferCapacity bounds how many causally-blocked messages may be held
	// simultaneously. 0 disables buffering (immediate reject on missing pred).
	BufferCapacity int `json:"buffer_capacity"`
	// MaxPayloadBytes rejects oversize envelopes before buffering. 0 = 1MiB.
	MaxPayloadBytes int `json:"max_payload_bytes"`

	// StoreDriver is "memory" or "postgres".
	StoreDriver string `json:"store_driver"`
	// PostgresDSN is used when StoreDriver == "postgres".
	PostgresDSN string `json:"postgres_dsn"`
	// TablePrefix namespaces tables in a shared database.
	TablePrefix string `json:"table_prefix"`

	// DecisionLogDir receives per-run structured decision logs; "" disables.
	DecisionLogDir string `json:"decision_log_dir"`
}

// fileConfig is the on-disk shape (no different fields today, but kept
// explicit so file defaults can be distinguished from zero values).
type fileConfig struct {
	NodeID          string `json:"node_id"`
	ListenAddr      string `json:"listen_addr"`
	Peers           []Peer `json:"peers"`
	BufferCapacity  int    `json:"buffer_capacity"`
	MaxPayloadBytes int    `json:"max_payload_bytes"`
	StoreDriver     string `json:"store_driver"`
	PostgresDSN     string `json:"postgres_dsn"`
	TablePrefix     string `json:"table_prefix"`
	DecisionLogDir  string `json:"decision_log_dir"`
}

// Default returns a config with safe defaults filled in.
func Default() Config {
	return Config{
		ListenAddr:      ":8080",
		BufferCapacity:  256,
		MaxPayloadBytes: 1 << 20,
		StoreDriver:     "memory",
		TablePrefix:     "cbcast",
	}
}

// Load reads an optional JSON config file and then applies environment
// overrides (CBCAST_*). Empty file path is valid: env/defaults only.
func Load(filePath string) (Config, error) {
	cfg := Default()

	if filePath != "" {
		raw, err := os.ReadFile(filePath)
		if err != nil {
			return cfg, fmt.Errorf("read config %q: %w", filePath, err)
		}
		var fc fileConfig
		if err := json.Unmarshal(raw, &fc); err != nil {
			return cfg, fmt.Errorf("parse config %q: %w", filePath, err)
		}
		applyFile(&cfg, fc)
	}

	applyEnv(&cfg)

	if err := cfg.Validate(); err != nil {
		return cfg, err
	}
	cfg.canonicalize()
	return cfg, nil
}

func applyFile(c *Config, f fileConfig) {
	if f.NodeID != "" {
		c.NodeID = f.NodeID
	}
	if f.ListenAddr != "" {
		c.ListenAddr = f.ListenAddr
	}
	if len(f.Peers) > 0 {
		c.Peers = append([]Peer(nil), f.Peers...)
	}
	if f.BufferCapacity > 0 {
		c.BufferCapacity = f.BufferCapacity
	}
	if f.MaxPayloadBytes > 0 {
		c.MaxPayloadBytes = f.MaxPayloadBytes
	}
	if f.StoreDriver != "" {
		c.StoreDriver = f.StoreDriver
	}
	if f.PostgresDSN != "" {
		c.PostgresDSN = f.PostgresDSN
	}
	if f.TablePrefix != "" {
		c.TablePrefix = f.TablePrefix
	}
	if f.DecisionLogDir != "" {
		c.DecisionLogDir = f.DecisionLogDir
	}
}

func env(k string) (string, bool) { return os.LookupEnv("CBCAST_" + k) }

func applyEnv(c *Config) {
	if v, ok := env("NODE_ID"); ok {
		c.NodeID = v
	}
	if v, ok := env("LISTEN_ADDR"); ok {
		c.ListenAddr = v
	}
	if v, ok := env("PEERS"); ok {
		// id=addr,id=addr,...
		c.Peers = c.Peers[:0]
		for _, item := range strings.Split(v, ",") {
			item = strings.TrimSpace(item)
			if item == "" {
				continue
			}
			kv := strings.SplitN(item, "=", 2)
			p := Peer{ID: strings.TrimSpace(kv[0])}
			if len(kv) == 2 {
				p.Address = strings.TrimSpace(kv[1])
			}
			c.Peers = append(c.Peers, p)
		}
	}
	if v, ok := env("BUFFER_CAPACITY"); ok {
		fmt.Sscanf(v, "%d", &c.BufferCapacity)
	}
	if v, ok := env("MAX_PAYLOAD_BYTES"); ok {
		fmt.Sscanf(v, "%d", &c.MaxPayloadBytes)
	}
	if v, ok := env("STORE_DRIVER"); ok {
		c.StoreDriver = v
	}
	if v, ok := env("POSTGRES_DSN"); ok {
		c.PostgresDSN = v
	}
	if v, ok := env("TABLE_PREFIX"); ok {
		c.TablePrefix = v
	}
	if v, ok := env("DECISION_LOG_DIR"); ok {
		c.DecisionLogDir = v
	}
}

// Members returns the membership as an ID set plus the canonical (sorted) ID
// list and address map.
func (c Config) Members() (map[string]bool, []string, map[string]string) {
	set := make(map[string]bool, len(c.Peers))
	addrs := make(map[string]string, len(c.Peers))
	ids := make([]string, 0, len(c.Peers))
	for _, p := range c.Peers {
		if set[p.ID] {
			continue // duplicates rejected in Validate
		}
		set[p.ID] = true
		addrs[p.ID] = p.Address
		ids = append(ids, p.ID)
	}
	sort.Strings(ids)
	return set, ids, addrs
}

func (c *Config) canonicalize() {
	sort.Slice(c.Peers, func(i, j int) bool { return c.Peers[i].ID < c.Peers[j].ID })
}

// Validate checks the configuration as a whole.
func (c Config) Validate() error {
	var problems []string
	if c.NodeID == "" {
		problems = append(problems, "node_id is required")
	}
	if len(c.Peers) < 2 {
		problems = append(problems, "at least two peers are required for a causal group")
	}
	seen := map[string]bool{}
	localFound := false
	for _, p := range c.Peers {
		if p.ID == "" {
			problems = append(problems, "peer with empty id")
			continue
		}
		if seen[p.ID] {
			problems = append(problems, fmt.Sprintf("duplicate peer id %q", p.ID))
		}
		seen[p.ID] = true
		if p.ID == c.NodeID {
			localFound = true
		}
	}
	if c.NodeID != "" && !localFound {
		problems = append(problems, fmt.Sprintf("node_id %q is not listed among peers", c.NodeID))
	}
	if c.BufferCapacity < 0 {
		problems = append(problems, "buffer_capacity must be >= 0")
	}
	if c.MaxPayloadBytes < 0 {
		problems = append(problems, "max_payload_bytes must be >= 0")
	}
	switch c.StoreDriver {
	case "memory", "postgres":
	default:
		problems = append(problems, fmt.Sprintf("unknown store_driver %q (want memory|postgres)", c.StoreDriver))
	}
	if c.StoreDriver == "postgres" && strings.TrimSpace(c.PostgresDSN) == "" {
		problems = append(problems, "postgres_dsn is required when store_driver=postgres")
	}
	if c.TablePrefix == "" {
		problems = append(problems, "table_prefix must not be empty")
	}
	if len(problems) > 0 {
		return fmt.Errorf("invalid configuration:\n  - %s", strings.Join(problems, "\n  - "))
	}
	return nil
}
