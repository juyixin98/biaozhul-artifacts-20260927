// Package config loads the startup configuration and constructs the ordered
// mutator/validator chain. Failure policy and timeout are explicit per plugin
// in the file — plugins never decide open/close for themselves.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"admission/internal/admission"
	"admission/internal/plugins"
	"admission/internal/quota"
	"admission/internal/types"
)

// File is the on-disk configuration.
type File struct {
	HTTP      HTTPConfig      `json:"http"`
	Storage   StorageConfig   `json:"storage"`
	Logging   LoggingConfig   `json:"logging"`
	Admission AdmissionConfig `json:"admission"`
	Reconcile ReconcileConfig `json:"reconcile"`
}

// HTTPConfig configures the stdlib HTTP server.
type HTTPConfig struct {
	Addr              string `json:"addr"`
	ReadTimeoutMS     int    `json:"readTimeoutMs"`
	WriteTimeoutMS    int    `json:"writeTimeoutMs"`
	ShutdownTimeoutMS int    `json:"shutdownTimeoutMs"`
}

// StorageConfig configures SQLite.
type StorageConfig struct {
	SQLitePath string `json:"sqlitePath"` // :memory: allowed for demos
}

// LoggingConfig configures replay logs.
type LoggingConfig struct {
	Dir string `json:"dir"`
}

// AdmissionConfig configures the chain.
type AdmissionConfig struct {
	DefaultTimeoutMS  int            `json:"defaultTimeoutMs"`
	MaxMutationPasses int            `json:"maxMutationPasses"`
	Defaults          []PluginConfig `json:"defaults"`
	Mutators          []PluginConfig `json:"mutators"`
	Validators        []PluginConfig `json:"validators"`
}

// ReconcileConfig configures retries.
type ReconcileConfig struct {
	MaxAttempts int     `json:"maxAttempts"`
	BaseDelayMS int     `json:"baseDelayMs"`
	Factor      float64 `json:"factor"`
	MaxDelayMS  int     `json:"maxDelayMs"`
	TickMS      int     `json:"tickMs"`
}

// PluginConfig is one chain entry.
type PluginConfig struct {
	Type          string                  `json:"type"`
	Name          string                  `json:"name,omitempty"`
	TimeoutMS     int                     `json:"timeoutMs,omitempty"`
	FailurePolicy admission.FailurePolicy `json:"failurePolicy"`
	// Args carries type-specific parameters.
	Args map[string]any `json:"args,omitempty"`
}

// Built is the constructed runtime wiring.
type Built struct {
	Pipeline *admission.Pipeline
	Config   File
	Ledger   quota.Adapter // nil unless quota is configured
}

// Load reads and validates a configuration file.
func Load(path string) (File, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return File{}, fmt.Errorf("read config %s: %w", path, err)
	}
	var f File
	if err := json.Unmarshal(b, &f); err != nil {
		return File{}, fmt.Errorf("parse config %s: %w", path, err)
	}
	applyFileDefaults(&f)
	if err := validate(f); err != nil {
		return File{}, err
	}
	return f, nil
}

func validate(f File) error {
	if f.Storage.SQLitePath == "" {
		return fmt.Errorf("config: storage.sqlitePath is required")
	}
	if len(f.Admission.Validators) == 0 {
		return fmt.Errorf("config: at least one validator is required")
	}
	names := map[string]bool{}
	all := append(append([]PluginConfig{}, f.Admission.Defaults...), f.Admission.Mutators...)
	all = append(all, f.Admission.Validators...)
	for _, p := range all {
		if p.Type == "" {
			return fmt.Errorf("config: plugin type is required")
		}
		if p.FailurePolicy != admission.FailOpen && p.FailurePolicy != admission.FailClose {
			return fmt.Errorf("config: plugin %q must declare failurePolicy FailOpen or FailClose", p.Type)
		}
		key := p.Type
		if p.Name != "" {
			key = p.Name
		}
		if names[key] {
			return fmt.Errorf("config: duplicate plugin key %q", key)
		}
		names[key] = true
	}
	return nil
}

func applyFileDefaults(f *File) {
	if f.HTTP.Addr == "" {
		f.HTTP.Addr = ":8080"
	}
	if f.HTTP.ReadTimeoutMS == 0 {
		f.HTTP.ReadTimeoutMS = 2000
	}
	if f.HTTP.WriteTimeoutMS == 0 {
		f.HTTP.WriteTimeoutMS = 3000
	}
	if f.HTTP.ShutdownTimeoutMS == 0 {
		f.HTTP.ShutdownTimeoutMS = 2000
	}
	if f.Logging.Dir == "" {
		f.Logging.Dir = "testlogs"
	}
	if f.Admission.DefaultTimeoutMS == 0 {
		f.Admission.DefaultTimeoutMS = admission.DefaultTimeoutMillis
	}
	if f.Admission.MaxMutationPasses == 0 {
		f.Admission.MaxMutationPasses = admission.DefaultMaxMutationPasses
	}
	if f.Reconcile.MaxAttempts == 0 {
		f.Reconcile.MaxAttempts = 4
	}
	if f.Reconcile.BaseDelayMS == 0 {
		f.Reconcile.BaseDelayMS = 50
	}
	if f.Reconcile.Factor == 0 {
		f.Reconcile.Factor = 2
	}
	if f.Reconcile.MaxDelayMS == 0 {
		f.Reconcile.MaxDelayMS = 1000
	}
	if f.Reconcile.TickMS == 0 {
		f.Reconcile.TickMS = 25
	}
}

// Build constructs the pipeline (and any local adapters) from a config file.
func Build(f File, logger admission.RunLogger) (*Built, error) {
	b := &Built{Config: f}
	cfg := admission.Config{
		DefaultTimeoutMS:  f.Admission.DefaultTimeoutMS,
		MaxMutationPasses: f.Admission.MaxMutationPasses,
	}

	for _, pc := range f.Admission.Defaults {
		m, err := buildMutator(pc, b)
		if err != nil {
			return nil, err
		}
		cfg.Defaults = append(cfg.Defaults, admission.MutatorSpec{
			Plugin: m, TimeoutMS: pc.TimeoutMS, FailurePolicy: pc.FailurePolicy,
		})
	}
	for _, pc := range f.Admission.Mutators {
		m, err := buildMutator(pc, b)
		if err != nil {
			return nil, err
		}
		cfg.Mutators = append(cfg.Mutators, admission.MutatorSpec{
			Plugin: m, TimeoutMS: pc.TimeoutMS, FailurePolicy: pc.FailurePolicy,
		})
	}
	for _, pc := range f.Admission.Validators {
		v, err := buildValidator(pc, b)
		if err != nil {
			return nil, err
		}
		cfg.Validators = append(cfg.Validators, admission.ValidatorSpec{
			Plugin: v, TimeoutMS: pc.TimeoutMS, FailurePolicy: pc.FailurePolicy,
		})
	}

	pipe, err := admission.New(cfg, logger)
	if err != nil {
		return nil, err
	}
	b.Pipeline = pipe
	return b, nil
}

func buildMutator(pc PluginConfig, b *Built) (admission.Mutator, error) {
	switch pc.Type {
	case "defaults.replicas":
		return &plugins.ReplicaDefaulter{Default: intArg(pc.Args, "default", 3)}, nil
	case "defaults.resources":
		return &plugins.ResourcesDefaulter{
			DefaultCPU:    strArg(pc.Args, "defaultCPU", "250m"),
			DefaultMemory: strArg(pc.Args, "defaultMemory", "128Mi"),
		}, nil
	case "mutators.reserved-resources":
		return &plugins.ReservedResources{}, nil
	case "mutators.label-sync":
		return &plugins.LabelSync{}, nil
	case "fixtures.delay":
		return &plugins.DelayPlugin{
			Name_:        key(pc),
			Delay:        time.Duration(intArg(pc.Args, "delayMs", 1000)) * time.Millisecond,
			AllowedPaths: []string{"/spec/extra/delay"},
		}, nil
	case "fixtures.flaky":
		return &plugins.FlakyMutator{
			Name_:        key(pc),
			Failures:     intArg(pc.Args, "failures", 1),
			FailCategory: types.Category(catArg(pc.Args, "category")),
			AllowedPaths: []string{"/spec/extra/flaky"},
		}, nil
	case "fixtures.oscillating":
		return &plugins.OscillatingMutator{}, nil
	case "fixtures.rogue":
		return &plugins.RogueMutator{Name_: key(pc), Attack: strArg(pc.Args, "attack", "outside")}, nil
	default:
		return nil, fmt.Errorf("config: unknown mutator type %q", pc.Type)
	}
}

func buildValidator(pc PluginConfig, b *Built) (admission.Validator, error) {
	switch pc.Type {
	case "validators.replica-range":
		return &plugins.ReplicaRangeValidator{
			Min: intArg(pc.Args, "min", 1),
			Max: intArg(pc.Args, "max", 10),
		}, nil
	case "validators.immutable-fields":
		return &plugins.ImmutableValidator{}, nil
	case "validators.quota":
		cpu := intArg(pc.Args, "capacityCPUm", 1000)
		mem := intArg(pc.Args, "capacityMemoryBytes", 1<<30)
		b.Ledger = quota.NewMemoryLedger(cpu, int64(mem))
		return &plugins.QuotaValidator{Adapter: b.Ledger}, nil
	case "fixtures.delay-validator":
		return &plugins.DelayValidator{
			Name_: key(pc),
			Delay: time.Duration(intArg(pc.Args, "delayMs", 1000)) * time.Millisecond,
		}, nil
	default:
		return nil, fmt.Errorf("config: unknown validator type %q", pc.Type)
	}
}

func key(pc PluginConfig) string {
	if pc.Name != "" {
		return pc.Name
	}
	return pc.Type
}

func intArg(args map[string]any, k string, def int) int {
	if args == nil {
		return def
	}
	v, ok := args[k]
	if !ok {
		return def
	}
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	}
	return def
}

func strArg(args map[string]any, k, def string) string {
	if args == nil {
		return def
	}
	if v, ok := args[k].(string); ok {
		return v
	}
	return def
}

func catArg(args map[string]any, k string) (cat string) {
	return strArg(args, k, "")
}
