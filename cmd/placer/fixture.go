package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"placer/internal/logx"
	"placer/internal/model"
	"placer/internal/store"
)

// fixtureFile is the synthetic cluster fixture format loaded at startup or
// by tests. It is fully local: no production accounts or real data.
type fixtureFile struct {
	Name      string           `json:"name"`
	Nodes     []model.Node     `json:"nodes"`
	Instances []model.Instance `json:"instances"`
	Policy    model.Policy     `json:"policy"`
}

func loadFixture(ctx context.Context, st *store.Store, path string, log *logx.Logger) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read fixture: %w", err)
	}
	var fx fixtureFile
	if err := json.Unmarshal(data, &fx); err != nil {
		return fmt.Errorf("parse fixture: %w", err)
	}
	if fx.Name != "" {
		log.Info("fixture_loading", map[string]any{"fixture": fx.Name, "path": path})
	}
	if err := st.Reset(ctx); err != nil {
		return err
	}
	for i := range fx.Nodes {
		if err := st.UpsertNode(ctx, fx.Nodes[i]); err != nil {
			return fmt.Errorf("fixture node %d: %w", i, err)
		}
	}
	for i := range fx.Instances {
		in := fx.Instances[i]
		if in.State == "" {
			if in.NodeID != "" {
				in.State = model.StateBound
			} else {
				in.State = model.StatePending
			}
		}
		if err := st.UpsertInstance(ctx, in); err != nil {
			return fmt.Errorf("fixture instance %d: %w", i, err)
		}
	}
	if len(fx.Policy.Groups) > 0 {
		if err := st.SetPolicy(ctx, fx.Policy); err != nil {
			return err
		}
	}
	log.Info("fixture_loaded", map[string]any{
		"nodes": len(fx.Nodes), "instances": len(fx.Instances),
		"rules": len(fx.Policy.Groups),
	})
	return nil
}
