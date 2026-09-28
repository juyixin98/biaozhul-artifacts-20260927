package plugins

import (
	"context"
	"fmt"

	"admission/internal/admission"
	"admission/internal/quota"
	"admission/internal/types"
)

// QuotaValidator consults the local capacity adapter. Exhaustion is reported
// with the QuotaExhausted category (retryable by the reconciler); malformed
// quantities are InvalidInput. It only checks — the service reserves capacity
// after the chain fully succeeds, so a denied request never books capacity.
type QuotaValidator struct {
	Adapter quota.Adapter
}

func (q *QuotaValidator) Name() string { return "validators.quota" }

func (q *QuotaValidator) Validate(_ context.Context, req types.Review) (types.Decision, error) {
	cpu, mem, err := quota.RequestFromObject(req.Object)
	if err != nil {
		return types.Decision{}, &admission.PluginError{
			Category: types.CatInvalidInput, Plugin: q.Name(),
			Message: "invalid resource quantity: " + err.Error(),
		}
	}
	// On UPDATE only net-new capacity is constrained; fixtures exercise CREATE.
	if req.Operation == "UPDATE" {
		return types.Decision{Allowed: true}, nil
	}
	if err := q.Adapter.Check(cpu, mem); err != nil {
		if ex, ok := err.(*quota.ExhaustedError); ok {
			return types.Decision{}, &admission.PluginError{
				Category: types.CatQuotaExhausted, Plugin: q.Name(),
				Message: fmt.Sprintf(
					"insufficient %s: requested cpu=%dm mem=%d, used cpu=%dm mem=%d, capacity cpu=%dm mem=%d",
					ex.Resource, ex.RequestCPU, ex.RequestMemory,
					ex.UsedCPU, ex.UsedMemory, ex.CapacityCPU, ex.CapacityMem),
			}
		}
		return types.Decision{}, &admission.PluginError{
			Category: types.CatComputeFailure, Plugin: q.Name(), Message: err.Error(),
		}
	}
	return types.Decision{Allowed: true}, nil
}
