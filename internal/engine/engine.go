// Package engine wires persistence, scheduling and logging: it converts stored
// cluster records into scheduler inputs, executes the solve, and persists the
// outcome (success placements or structured failure) with full decision steps.
package engine

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"

	"opp284/placement/internal/logging"
	"opp284/placement/internal/model"
	"opp284/placement/internal/scheduler"
	"opp284/placement/internal/store"
)

// PlanInput is the API/loop-level request to compute a plan.
type PlanInput struct {
	ClusterID     string
	Intents       []model.Intent
	Replacements  map[string]scheduler.Replacement
	AllowRecreate bool
}

// PlanOutput is the outcome of one plan computation.
type PlanOutput struct {
	PlanID    string
	Success   bool
	Decision  *scheduler.Decision
	Failure   *model.PlanFailure
	Steps     []scheduler.Step
}

// Engine owns one store and the scheduler limits.
type Engine struct {
	st       *store.Store
	limits   scheduler.SearchLimits
	log      *logging.Logger
}

// New constructs an engine.
func New(st *store.Store, limits scheduler.SearchLimits, log *logging.Logger) *Engine {
	return &Engine{st: st, limits: limits, log: log}
}

// NewPlanID returns an opaque random 16-byte hex id.
func NewPlanID() string { return "pl-" + randHex(8) }

// NewRequestID returns an opaque correlation id.
func NewRequestID() string { return "rq-" + randHex(8) }

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// crypto/rand failing is catastrophic; surface a fixed marker rather
		// than silently continuing.
		return "0000000000000000"
	}
	return hex.EncodeToString(b)
}

// SolveAndSave computes and persists a plan. Failures are persisted with
// status=failed and returned; callers must not treat a returned failure as a
// success. The logger emits explicit ok/failed events with correlation ids.
func (e *Engine) SolveAndSave(ctx context.Context, requestID string, in PlanInput) (*PlanOutput, error) {
	planID := NewPlanID()
	l := e.log.With(map[string]any{"request_id": requestID, "plan_id": planID, "cluster_id": in.ClusterID})

	nodes, running, groups, zones, err := e.st.LoadCluster(ctx, in.ClusterID)
	if err != nil {
		l.Fail("load cluster", map[string]any{"err": err.Error()})
		return nil, fmt.Errorf("load cluster %s: %w", in.ClusterID, err)
	}
	snap := scheduler.Snapshot{
		Nodes:         nodes,
		DeclaredZones: zones,
		Groups:        groups,
		Running:       make([]scheduler.RunningInstance, 0, len(running)),
	}
	for _, r := range running {
		snap.Running = append(snap.Running, scheduler.RunningInstance{
			ID: r.ID, NodeID: r.NodeID, Request: r.Request, AffinityGroups: r.Groups,
		})
	}
	req := scheduler.Request{
		PlanID:        planID,
		Intents:       in.Intents,
		Replacements:  in.Replacements,
		AllowRecreate: in.AllowRecreate,
	}

	l.Debug("solve start", map[string]any{
		"intents":     len(in.Intents),
		"nodes":       len(nodes),
		"running":     len(running),
		"recreate":    in.AllowRecreate,
	})
	decision, steps, fail := scheduler.Solve(snap, req, e.limits)

	out := &PlanOutput{PlanID: planID, Steps: steps}
	if fail != nil {
		out.Failure = fail
		l.Fail("solve constraint conflict", map[string]any{
			"code":       string(fail.Code),
			"instances":  len(fail.Instances),
			"steps":      len(steps),
		})
		if perr := e.st.SavePlan(ctx, store.PlanRecord{
			ClusterID:     in.ClusterID,
			PlanID:        planID,
			RequestID:     requestID,
			Status:        "failed",
			Score:         scheduler.ScoreVector{},
			Domains:       nil,
			Failure:       fail,
			AllowRecreate: in.AllowRecreate,
			Steps:         steps,
		}); perr != nil {
			l.Fail("persist failed plan", map[string]any{"err": perr.Error()})
			return nil, perr
		}
		return out, nil
	}

	out.Success = true
	out.Decision = decision
	l.OK("solve success", map[string]any{
		"exhaustive":      decision.Exhaustive,
		"leaf_visits":     decision.LeafVisits,
		"range":           decision.Score.ZoneCountRange,
		"variance_milli":  decision.Score.ZoneCountVariance,
		"max_util_milli":  decision.Score.MaxNodeUtil,
		"domains":         fmt.Sprint(decision.ParticipatingDomains),
	})
	if perr := e.st.SavePlan(ctx, store.PlanRecord{
		ClusterID:     in.ClusterID,
		PlanID:        planID,
		RequestID:     requestID,
		Status:        "succeeded",
		Exhaustive:    decision.Exhaustive,
		LeafVisits:    decision.LeafVisits,
		Score:         decision.Score,
		Domains:       decision.ParticipatingDomains,
		Placements:    decision.Placements,
		Steps:         steps,
		AllowRecreate: in.AllowRecreate,
	}); perr != nil {
		l.Fail("persist plan", map[string]any{"err": perr.Error()})
		return nil, perr
	}
	return out, nil
}
