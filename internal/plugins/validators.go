package plugins

import (
	"context"
	"errors"
	"fmt"
	"time"

	"admission/internal/model"
)

// SchemaValidator enforces typed business rules on the final document. A
// violation is a policy denial (validation_denied), never an invocation
// error: the plugin decided correctly that the object is invalid.
type SchemaValidator struct {
	MaxReplicas int64
	TimeoutD    time.Duration
	Policy      FailPolicy
}

func (v *SchemaValidator) Name() string           { return "schema" }
func (v *SchemaValidator) Timeout() time.Duration { return v.TimeoutD }
func (v *SchemaValidator) OnError() FailPolicy    { return v.Policy }

func (v *SchemaValidator) Validate(_ context.Context, in Input) (Verdict, error) {
	res, err := model.ResourceFromMap(in.Doc)
	if err != nil {
		return Verdict{}, Fail(model.ReasonInvalidInput, "parse resource: %v", err)
	}
	if in.Request.Operation == model.OpDelete {
		return Verdict{Allowed: true}, nil
	}
	replicasRaw, ok := res.Spec["replicas"]
	if !ok {
		return Verdict{Allowed: false, Reason: model.ReasonValidationDenied,
			Message: "spec.replicas is required"}, nil
	}
	replicas, ok := toInt(replicasRaw)
	if !ok {
		return Verdict{Allowed: false, Reason: model.ReasonValidationDenied,
			Message: fmt.Sprintf("spec.replicas must be an integer, got %v", replicasRaw)}, nil
	}
	if replicas < 1 {
		return Verdict{Allowed: false, Reason: model.ReasonValidationDenied,
			Message: "spec.replicas must be >= 1"}, nil
	}
	if v.MaxReplicas > 0 && replicas > v.MaxReplicas {
		return Verdict{Allowed: false, Reason: model.ReasonValidationDenied,
			Message: fmt.Sprintf("spec.replicas %d exceeds per-object maximum %d", replicas, v.MaxReplicas)}, nil
	}
	capRaw, ok := res.Spec["capacity"]
	if !ok {
		return Verdict{Allowed: false, Reason: model.ReasonValidationDenied,
			Message: "spec.capacity is required after mutation"}, nil
	}
	capacity, ok := toInt(capRaw)
	if !ok {
		return Verdict{Allowed: false, Reason: model.ReasonValidationDenied,
			Message: "spec.capacity must be an integer"}, nil
	}
	if capacity < replicas {
		// Cross-field invariant: capacity must cover at least one unit per
		// replica. The default chain produces replicas*perReplica, so a
		// hand-crafted object with inconsistent values gets caught here —
		// proving final validation runs *after* the chain.
		return Verdict{Allowed: false, Reason: model.ReasonValidationDenied,
			Message: fmt.Sprintf("spec.capacity %d must be >= spec.replicas %d", capacity, replicas)}, nil
	}
	return Verdict{Allowed: true}, nil
}

// QuotaValidator reserves capacity against the QuotaService adapter. An
// exhausted ledger is a denial (resource_exhausted); adapter breakage is an
// invocation error subject to the explicit fail policy.
type QuotaValidator struct {
	Quota    QuotaService
	TimeoutD time.Duration
	Policy   FailPolicy
}

func (v *QuotaValidator) Name() string           { return "quota" }
func (v *QuotaValidator) Timeout() time.Duration { return v.TimeoutD }
func (v *QuotaValidator) OnError() FailPolicy    { return v.Policy }

func (v *QuotaValidator) Validate(ctx context.Context, in Input) (Verdict, error) {
	if v.Quota == nil {
		return Verdict{}, Fail(model.ReasonComputeFailure, "quota service is not wired")
	}
	if in.Request.Operation == model.OpDelete {
		return Verdict{Allowed: true}, nil
	}
	res, err := model.ResourceFromMap(in.Doc)
	if err != nil {
		return Verdict{}, Fail(model.ReasonInvalidInput, "parse resource: %v", err)
	}
	replicas, ok := toInt(res.Spec["replicas"])
	if !ok {
		return Verdict{}, Fail(model.ReasonInvalidInput, "spec.replicas missing or non-integer")
	}
	if in.Request.Operation == model.OpUpdate {
		// Only scale-up consumes additional quota.
		old, err := model.ResourceFromMap(in.Request.OldObject)
		if err != nil || old.Spec == nil {
			return Verdict{}, Fail(model.ReasonInvalidInput, "UPDATE requires oldObject.spec for quota delta")
		}
		oldReplicas, _ := toInt(old.Spec["replicas"])
		replicas -= oldReplicas
	}
	if replicas <= 0 {
		return Verdict{Allowed: true}, nil // scale-down/unchanged: nothing to reserve
	}
	if err := v.Quota.Reserve(ctx, in.Request.UID, res.Kind, replicas, in.Request.Operation); err != nil {
		if IsQuotaExhausted(err) {
			return Verdict{Allowed: false, Reason: model.ReasonResourceExhausted,
				Message: err.Error()}, nil
		}
		return Verdict{}, err
	}
	return Verdict{Allowed: true}, nil
}

// IsQuotaExhausted reports whether an adapter error is the exhaustion signal.
func IsQuotaExhausted(err error) bool {
	return errors.Is(err, ErrQuotaExhausted)
}

var _ Validator = (*SchemaValidator)(nil)
var _ Validator = (*QuotaValidator)(nil)
