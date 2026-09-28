// Package provider defines the resource adapter contract and ships a
// fully in-process simulated provider. Real cloud adapters would implement
// the same Provider interface; nothing above it knows the world is simulated.
package provider

import (
	"context"
	"sync"

	"infraplanner/internal/model"
)

// CreateResult is the provider response to a successful create. The physical
// ID it carries is what distinguishes a replace from an in-place update.
type CreateResult struct {
	ID   string
	Live model.Live
}

// Provider is the adapter contract.
type Provider interface {
	Observe(ctx context.Context) (model.Observation, error)
	Create(ctx context.Context, d model.Desired, resolve RefResolver) (CreateResult, error)
	Update(ctx context.Context, id string, d model.Desired, resolve RefResolver) (model.Live, error)
	Delete(ctx context.Context, id string, k model.Key) error
}

// RefResolver maps a declared logical reference to a concrete physical ID.
// The reconciler supplies it from resources it has already created during
// the run, falling back to the baseline observation for pre-existing ones.
type RefResolver func(ref model.Ref) (physicalID string, err error)

// EnsureError wraps an error with the model failure category. Provider
// backends return plain sentinel errors; the adapter maps them here so the
// category contract is enforced in one place.
func EnsureError(err error) error {
	if err == nil {
		return nil
	}
	if _, ok := model.AsError(err); ok {
		return err
	}
	return model.E(model.CatCompute, "provider_error", "%v", err)
}

// memory is the simulated control-plane state.
type memory struct {
	mu        sync.Mutex
	byID      map[string]*model.Live
	nameIndex map[model.Key]string // logical key -> physical ID
	seq       int
	// capacity: max resources of a kind, 0 = unlimited.
	capacity map[model.Kind]int
}

func newMemory() *memory {
	return &memory{
		byID:      map[string]*model.Live{},
		nameIndex: map[model.Key]string{},
		capacity:  map[model.Kind]int{},
	}
}
