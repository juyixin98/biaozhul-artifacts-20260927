// Package observe wraps an adapter Provider with run stamping. A run number is
// the replay identifier carried through plan binding, evidence and test logs.
package observe

import (
	"context"

	"infraplanner/internal/adapter"
	"infraplanner/internal/model"
)

// Observer reads actual resource state and stamps it with a run number.
type Observer struct {
	prov adapter.Provider
}

func New(prov adapter.Provider) *Observer { return &Observer{prov: prov} }

// Snapshot performs an authoritative observation. run is assigned by storage
// (monotonic) so it is stable across process restarts.
func (o *Observer) Snapshot(ctx context.Context, run int64) (*model.ObservedSet, error) {
	set, err := o.prov.Observe(ctx)
	if err != nil {
		return nil, err
	}
	set.Run = run
	return set, nil
}
