// Package config parses and validates the static routing configuration file.
//
// The file format is YAML (see configs/config.example.yaml). Parsing is kept
// separate from the network model: this package never opens sockets, never
// builds rings and never touches the state store.
package config

import (
	"fmt"
	"net/netip"
	"os"
	"time"

	"gopkg.in/yaml.v3"

	"flowrouter/internal/apperr"
)

// Member describes one candidate next-hop declared in the file.
type Member struct {
	// ID is the stable identity of the member. It participates in hash
	// tie-breaking, so changing an ID (not just its weight) intentionally
	// remaps buckets: the member is a different identity.
	ID string `yaml:"id"`
	// Address is where traffic would be forwarded in a real deployment. Here
	// it is used only for optional liveness probes (host:port).
	Address string `yaml:"address"`
	// Weight is a non-negative integer share of the hash ring. Weight 0 is
	// legal: the member is tracked (health/admin operations still address it)
	// but owns zero buckets and zero traffic.
	Weight int `yaml:"weight"`
}

// HealthConfig controls optional active TCP liveness probing. Probing never
// marks a member up; it only marks members down. Recovery is explicit and
// versioned (see internal/router), matching the mandated semantics.
type HealthConfig struct {
	// Enabled turns the background prober on.
	Enabled bool `yaml:"enabled"`
	// Interval is the probe period (YAML duration, e.g. "1s").
	Interval Duration `yaml:"interval"`
	// Timeout is the per-probe dial deadline (YAML duration, e.g. "200ms").
	Timeout Duration `yaml:"timeout"`
	// Failures is the consecutive failed probes required to mark a member down.
	Failures int `yaml:"failures"`
}

// Config is the complete parsed configuration.
type Config struct {
	Listen          string `yaml:"listen"`
	VNodesPerWeight int    `yaml:"vnodes_per_weight"`
	// MaxVNodes caps the total number of vnodes on the ring. When set (>0),
	// weight-to-vnode counts are reduced with a documented largest-remainder
	// integer allocation that guarantees at least one vnode per positive
	// member while the cap allows it. Zero means "no cap".
	MaxVNodes int          `yaml:"max_vnodes"`
	Store     StoreConfig  `yaml:"store"`
	Health    HealthConfig `yaml:"health"`
	Members   []Member     `yaml:"members"`
}

type StoreConfig struct {
	// Path is the SQLite database path. ":memory:" yields an ephemeral DB.
	Path string `yaml:"path"`
	// BusyTimeoutMs bounds how long SQLite waits on a locked database before
	// returning SQLITE_BUSY (surfaced as RESOURCE_EXHAUSTED/STORE_BUSY).
	BusyTimeoutMs int `yaml:"busy_timeout_ms"`
}

// Duration wraps time.Duration so it unmarshals from a YAML string like "500ms".
type Duration struct {
	time.Duration
}

func (d *Duration) UnmarshalYAML(value *yaml.Node) error {
	var s string
	if err := value.Decode(&s); err != nil {
		return err
	}
	parsed, err := time.ParseDuration(s)
	if err != nil {
		return apperr.Invalid("BAD_DURATION", fmt.Sprintf("invalid duration %q: %v", s, err))
	}
	d.Duration = parsed
	return nil
}

// Load reads, parses and validates a configuration file.
func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, apperr.Invalid("CONFIG_UNREADABLE",
			fmt.Sprintf("cannot read config file %q: %v", path, err)).WithCause(err)
	}
	cfg := Defaults()
	if err := yaml.Unmarshal(raw, cfg); err != nil {
		if ae, ok := apperr.As(err); ok {
			return nil, ae
		}
		return nil, apperr.Invalid("CONFIG_MALFORMED",
			fmt.Sprintf("config %q is not valid YAML: %v", path, err)).WithCause(err)
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Defaults returns the configuration populated with default values.
func Defaults() *Config {
	return &Config{
		Listen:          "127.0.0.1:8080",
		VNodesPerWeight: 160,
		Store: StoreConfig{
			Path:          "flowrouter.db",
			BusyTimeoutMs: 1000,
		},
		Health: HealthConfig{
			Enabled:  false,
			Interval: Duration{time.Second},
			Timeout:  Duration{200 * time.Millisecond},
			Failures: 1,
		},
	}
}

// Validate enforces structural rules. It is deliberately tolerant of
// operationally-dangerous-but-meaningful states:
//
//   - an empty member set is accepted (service starts; routing returns
//     NO_HEALTHY_MEMBER/NO_MEMBERS),
//   - all-zero weights are accepted (ring is empty; routing returns
//     NO_HEALTHY_MEMBER/ZERO_TOTAL_WEIGHT).
//
// The startup flag -check-config can be used by operators to notice these.
func (c *Config) Validate() error {
	if c.Listen == "" {
		return apperr.Invalid("EMPTY_LISTEN", "listen must not be empty")
	}
	if c.VNodesPerWeight < 1 {
		return apperr.Invalid("BAD_VNODES_PER_WEIGHT",
			"vnodes_per_weight must be >= 1")
	}
	if c.MaxVNodes < 0 {
		return apperr.Invalid("BAD_MAX_VNODES", "max_vnodes must be >= 0 (0 disables the cap)")
	}
	if c.Store.BusyTimeoutMs < 0 {
		return apperr.Invalid("BAD_BUSY_TIMEOUT", "store.busy_timeout_ms must be >= 0")
	}
	if c.Health.Enabled {
		if c.Health.Interval.Duration <= 0 {
			return apperr.Invalid("BAD_PROBE_INTERVAL", "health.interval must be > 0 when probing is enabled")
		}
		if c.Health.Timeout.Duration <= 0 {
			return apperr.Invalid("BAD_PROBE_TIMEOUT", "health.timeout must be > 0 when probing is enabled")
		}
		if c.Health.Failures < 1 {
			return apperr.Invalid("BAD_PROBE_FAILURES", "health.failures must be >= 1")
		}
	}

	seen := make(map[string]bool, len(c.Members))
	positive := 0
	for i, m := range c.Members {
		at := fmt.Sprintf("members[%d]", i)
		if m.ID == "" {
			return apperr.Invalid("MEMBER_EMPTY_ID", at+": id must not be empty")
		}
		if seen[m.ID] {
			return apperr.Invalid("MEMBER_DUPLICATE_ID",
				fmt.Sprintf("%s: duplicate member id %q", at, m.ID))
		}
		seen[m.ID] = true
		if m.Weight < 0 {
			return apperr.Invalid("MEMBER_NEGATIVE_WEIGHT",
				fmt.Sprintf("%s(%s): weight must be >= 0", at, m.ID))
		}
		if m.Weight > 0 {
			positive++
		}
		if m.Address != "" {
			if _, err := netip.ParseAddrPort(m.Address); err != nil {
				return apperr.Invalid("MEMBER_BAD_ADDRESS",
					fmt.Sprintf("%s(%s): address must be host:port: %v", at, m.ID, err))
			}
		}
	}
	// A configured cap must be able to hold one vnode per positive-weight
	// member, otherwise the ring cannot satisfy the "every healthy positive
	// member gets traffic" guarantee.
	if c.MaxVNodes > 0 && positive > 0 && c.MaxVNodes < positive {
		return apperr.Invalid("CAP_TOO_SMALL",
			fmt.Sprintf("max_vnodes=%d cannot allocate at least one vnode to each of %d positive-weight members",
				c.MaxVNodes, positive))
	}
	return nil
}
