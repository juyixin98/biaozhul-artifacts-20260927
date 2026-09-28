package reconciler

import (
	"context"

	"infraplanner/internal/journal"
	"infraplanner/internal/model"
	"infraplanner/internal/planner"
	"infraplanner/internal/provider"
	"infraplanner/internal/spec"
)

// Plan validates the spec, observes the world, builds the plan and persists
// it as a new run in state "planned". Guard blocks do not raise an error; the
// run is persisted with PlanReady=false and must be re-planned with releases.
func (s *Service) Plan(ctx context.Context, req PlanRequest) (*PlanResponse, error) {
	lg, _ := s.openLogger("plan")
	defer closeLogger(lg)

	parsed, err := spec.Parse(req.Resources)
	if err != nil {
		if lg != nil {
			lg.Error("plan", "spec rejected", err, nil)
		}
		return nil, err
	}

	obs, err := s.prov.Observe(ctx)
	if err != nil {
		return nil, provider.EnsureError(err)
	}
	if lg != nil {
		lg.Info("observe", "world observed", map[string]any{"resources": len(obs.Resources)})
	}

	releases := map[model.Key]bool{}
	for _, k := range req.ReleaseGuards {
		releases[k] = true
	}

	plan, err := planner.Build(planner.Input{
		Spec:           parsed,
		Observed:       obs,
		ReleasedGuards: releases,
	})
	if err != nil {
		return nil, err
	}

	// Critical-resource guard: refuse to persist/apply any plan whose
	// destruction wave would remove a protected resource whose guard has not
	// been explicitly released. The caller must re-plan naming the keys.
	if len(plan.Guarded) > 0 {
		names := make([]string, 0, len(plan.Guarded))
		for _, k := range plan.Guarded {
			names = append(names, k.String())
		}
		ge := model.E(model.CatConflict, "guard_held",
			"plan blocked: %d protected resource(s) require explicit guard release: %v",
			len(plan.Guarded), names)
		if lg != nil {
			lg.Error("plan", "destruction guard held", ge, nil)
		}
		return nil, ge
	}

	runID := newRunID()
	if err := s.store.CreateRun(ctx, runID, req.Resources, req.ReleaseGuards); err != nil {
		return nil, model.E(model.CatCompute, "journal_write", "create run: %v", err)
	}
	if err := s.store.SetPlan(ctx, runID, plan.Fingerprint, encodePlan(plan)); err != nil {
		return nil, model.E(model.CatCompute, "journal_write", "persist plan: %v", err)
	}
	// seed op journal rows in pending order so resume can iterate them.
	for _, op := range plan.Operations {
		row := journal.OpRow{
			Seq: op.Seq, Type: string(op.Type), Key: op.Key,
			ExistingID: op.ExistingID, Detail: op.Detail,
			State: model.OpPending,
		}
		if err := s.store.UpsertOp(ctx, runID, row); err != nil {
			return nil, model.E(model.CatCompute, "journal_write", "seed op: %v", err)
		}
	}
	if lg != nil {
		lg.Info("plan", "plan persisted", map[string]any{
			"run_id": runID, "ops": len(plan.Operations),
			"guarded": len(plan.Guarded), "fingerprint": plan.Fingerprint,
		})
	}

	return &PlanResponse{
		RunID:       runID,
		Fingerprint: plan.Fingerprint,
		Observed:    len(obs.Resources),
		Operations:  plan.Operations,
		Guarded:     plan.Guarded,
		PlanReady:   len(plan.Guarded) == 0,
	}, nil
}

// GetRun loads a persisted run.
func (s *Service) GetRun(ctx context.Context, runID string) (*journal.Run, error) {
	return s.store.GetRun(ctx, runID)
}

// ListRuns lists recent runs.
func (s *Service) ListRuns(ctx context.Context, limit int) ([]*journal.Run, error) {
	return s.store.ListRuns(ctx, limit)
}

// Evidence returns persisted evidence for a run.
func (s *Service) Evidence(ctx context.Context, runID string) ([]journal.Evidence, error) {
	return s.store.ListEvidence(ctx, runID)
}

func closeLogger(l Logger) {
	if l != nil {
		_ = l.Close()
	}
}
