// Package config loads controller configuration from a local JSON file and
// applies documented defaults, so a run is reproducible from one checked-in
// artifact.
package config

import (
	"encoding/json"
	"fmt"
	"os"

	"replicactl/core/model"
)

// File is the on-disk JSON layout.
type File struct {
	TargetLoadPerInstance *float64 `json:"target_load_per_instance"`
	MaxScaleUpFactor      *float64 `json:"max_scale_up_factor"`
	MaxScaleUpFloor       *float64 `json:"max_scale_up_floor"`
	ScaleDownStableWindow *int64   `json:"scale_down_stable_window_seconds"`
	StaleSkew             *int64   `json:"stale_skew_seconds"`
	Tolerance             *float64 `json:"tolerance"`
	MinFreshFraction      *float64 `json:"min_fresh_fraction"`
	MinReplicas           *int     `json:"min_replicas"`
	MaxReplicas           *int     `json:"max_replicas"`
	BootstrapReplicas     *int     `json:"bootstrap_replicas"`

	// Runtime settings (not part of model.Config).
	HTTPAddr    string `json:"http_addr"`
	DatabaseDSN string `json:"database_dsn"`
}

// Load reads path and produces a validated model.Config plus runtime settings.
// Missing fields fall back to model.DefaultConfig(); runtime settings fall
// back to documented local defaults.
func Load(path string) (model.Config, Runtime, error) {
	cfg := model.DefaultConfig()
	rt := Runtime{HTTPAddr: "127.0.0.1:8080", DatabaseDSN: "file:data/controller.db"}
	if path == "" {
		return cfg, rt, cfg.Validate()
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, rt, fmt.Errorf("read config %q: %w", path, err)
	}
	var f File
	if err := json.Unmarshal(b, &f); err != nil {
		return cfg, rt, fmt.Errorf("parse config %q: %w", path, err)
	}
	if f.TargetLoadPerInstance != nil {
		cfg.TargetLoadPerInstance = *f.TargetLoadPerInstance
	}
	if f.MaxScaleUpFactor != nil {
		cfg.MaxScaleUpFactor = *f.MaxScaleUpFactor
	}
	if f.MaxScaleUpFloor != nil {
		cfg.MaxScaleUpFloor = *f.MaxScaleUpFloor
	}
	if f.ScaleDownStableWindow != nil {
		cfg.ScaleDownStableWindow = *f.ScaleDownStableWindow
	}
	if f.StaleSkew != nil {
		cfg.StaleSkew = *f.StaleSkew
	}
	if f.Tolerance != nil {
		cfg.Tolerance = *f.Tolerance
	}
	if f.MinFreshFraction != nil {
		cfg.MinFreshFraction = *f.MinFreshFraction
	}
	if f.MinReplicas != nil {
		cfg.MinReplicas = *f.MinReplicas
	}
	if f.MaxReplicas != nil {
		cfg.MaxReplicas = *f.MaxReplicas
	}
	if f.BootstrapReplicas != nil {
		cfg.BootstrapReplicas = *f.BootstrapReplicas
	}
	if f.HTTPAddr != "" {
		rt.HTTPAddr = f.HTTPAddr
	}
	if f.DatabaseDSN != "" {
		rt.DatabaseDSN = f.DatabaseDSN
	}
	if err := cfg.Validate(); err != nil {
		return cfg, rt, err
	}
	return cfg, rt, nil
}

// Runtime holds non-algorithm settings.
type Runtime struct {
	HTTPAddr    string
	DatabaseDSN string
}
