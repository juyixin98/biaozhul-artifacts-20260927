// Package app wires concrete plugins, storage and the coordinator together
// from a Config. It is the one composition root: cmd/admissiond and the
// integration tests both build their system through here.
package app

import (
	"context"
	"fmt"

	"admission/internal/config"
	"admission/internal/coordinator"
	"admission/internal/pipeline"
	"admission/internal/plugins"
	"admission/internal/runlog"
	"admission/internal/storage"
)

// System is the fully wired application.
type System struct {
	Store       *storage.Store
	Coordinator *coordinator.Coordinator
	Quota       *storage.SQLQuota
	RunLog      *runlog.Logger
}

// Build opens storage and constructs chains exactly in configured order.
func Build(ctx context.Context, cfg *config.Config, catalog plugins.DefaultsCatalog) (*System, error) {
	store, err := storage.Open(ctx, cfg.DatabasePath+"?_busy_timeout=5000&_foreign_keys=on")
	if err != nil {
		return nil, fmt.Errorf("open storage: %w", err)
	}
	logger, err := runlog.Open(cfg.LogDir)
	if err != nil {
		store.Close()
		return nil, fmt.Errorf("open run log: %w", err)
	}
	quota := storage.NewSQLQuota(store, cfg.QuotaLimits)

	mutators, err := buildMutators(cfg, catalog)
	if err != nil {
		return nil, err
	}
	validators, err := buildValidators(cfg, quota)
	if err != nil {
		return nil, err
	}
	pipe := pipeline.New(mutators, validators, cfg.MaxPasses)
	coord := coordinator.New(store, pipe, logger)
	return &System{Store: store, Coordinator: coord, Quota: quota, RunLog: logger}, nil
}

func buildMutators(cfg *config.Config, catalog plugins.DefaultsCatalog) ([]plugins.Mutator, error) {
	out := make([]plugins.Mutator, 0, len(cfg.Mutators))
	for _, pc := range cfg.Mutators {
		to := pc.Timeout()
		switch pc.Type {
		case "defaults":
			out = append(out, &plugins.DefaultsMutator{Catalog: catalog, TimeoutD: to, Policy: pc.Fail})
		case "capacity":
			per := pc.PerReplica
			if per == 0 {
				per = 100
			}
			out = append(out, &plugins.CapacityMutator{PerReplica: per, TimeoutD: to, Policy: pc.Fail})
		case "stamp-uid":
			out = append(out, &plugins.StampMutator{TimeoutD: to, Policy: pc.Fail})
		default:
			return nil, fmt.Errorf("unknown mutator %q", pc.Type)
		}
	}
	return out, nil
}

func buildValidators(cfg *config.Config, quota plugins.QuotaService) ([]plugins.Validator, error) {
	out := make([]plugins.Validator, 0, len(cfg.Validators))
	for _, pc := range cfg.Validators {
		to := pc.Timeout()
		switch pc.Type {
		case "schema":
			out = append(out, &plugins.SchemaValidator{MaxReplicas: pc.MaxReplicas, TimeoutD: to, Policy: pc.Fail})
		case "quota":
			out = append(out, &plugins.QuotaValidator{Quota: quota, TimeoutD: to, Policy: pc.Fail})
		default:
			return nil, fmt.Errorf("unknown validator %q", pc.Type)
		}
	}
	return out, nil
}

// Close releases resources.
func (s *System) Close() error {
	var first error
	if err := s.RunLog.Close(); err != nil && first == nil {
		first = err
	}
	if err := s.Store.Close(); err != nil && first == nil {
		first = err
	}
	return first
}
