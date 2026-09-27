package storage

import (
	"context"
	"errors"
	"time"

	"natlab/internal/model"
)

// Faulty wraps a Store and forces errors on selected method kinds. It exists so
// tests can observe the compute_failure class deterministically.
type Faulty struct {
	Inner Store
	// FailPutMapping forces PutMapping/UpdateMapping to fail.
	FailPutMapping bool
	// FailAppendDecision forces decision-log writes to fail.
	FailAppendDecision bool
}

// ErrInjected is the sentinel returned by Faulty on a forced failure.
var ErrInjected = errors.New("storage: injected fault")

func (f *Faulty) UpsertRun(ctx context.Context, r RunInfo) error {
	return f.Inner.UpsertRun(ctx, r)
}
func (f *Faulty) GetRun(ctx context.Context, id string) (RunInfo, error) {
	return f.Inner.GetRun(ctx, id)
}
func (f *Faulty) PutMapping(ctx context.Context, m StoredMapping) error {
	if f.FailPutMapping {
		return ErrInjected
	}
	return f.Inner.PutMapping(ctx, m)
}
func (f *Faulty) UpdateMapping(ctx context.Context, m StoredMapping) error {
	return f.PutMapping(ctx, m)
}
func (f *Faulty) DeleteMapping(ctx context.Context, runID, id string, at time.Time) error {
	return f.Inner.DeleteMapping(ctx, runID, id, at)
}
func (f *Faulty) ListMappings(ctx context.Context, runID string) ([]StoredMapping, error) {
	return f.Inner.ListMappings(ctx, runID)
}
func (f *Faulty) AddTombstone(ctx context.Context, t StoredTombstone) error {
	return f.Inner.AddTombstone(ctx, t)
}
func (f *Faulty) FindTombstone(ctx context.Context, runID string, proto model.Protocol,
	extPort uint16, remoteIP string, remotePort uint16, at time.Time) (bool, error) {
	return f.Inner.FindTombstone(ctx, runID, proto, extPort, remoteIP, remotePort, at)
}
func (f *Faulty) PruneTombstones(ctx context.Context, runID string, at time.Time) error {
	return f.Inner.PruneTombstones(ctx, runID, at)
}
func (f *Faulty) AppendDecision(ctx context.Context, d model.Decision) error {
	if f.FailAppendDecision {
		return ErrInjected
	}
	return f.Inner.AppendDecision(ctx, d)
}
func (f *Faulty) ListDecisions(ctx context.Context, runID string, fromSeq int64,
	limit int) ([]model.Decision, error) {
	return f.Inner.ListDecisions(ctx, runID, fromSeq, limit)
}
func (f *Faulty) SetWatermark(ctx context.Context, runID string, at time.Time) error {
	return f.Inner.SetWatermark(ctx, runID, at)
}
func (f *Faulty) Close() error { return f.Inner.Close() }
