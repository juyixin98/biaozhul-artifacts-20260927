package reconcile

import (
	"context"
	"errors"
)

// ExternalSpec is the desired state projected to the external service. It is
// a boundary type: the adapter maps model.WidgetSpec to it.
type ExternalSpec struct {
	Name        string
	Replicas    int
	Color       string
	SecretToken string
}

// ExternalResource is an observed instance in the actual resource service.
type ExternalResource struct {
	ID      string
	Name    string
	Version int64
	Spec    ExternalSpec
	SpecFP  string
}

// CallError is the normalized failure returned by the external adapter.
// Category lets the reconcile loop decide retry/requeue/refuse without
// parsing strings.
type CallError struct {
	// Category is one of: notFound, conflict, transient, ambiguous, rejected,
	// unknown.
	Category string
	Code     string
	Message  string
	Attempt  string // create|update|get|delete
}

func (e *CallError) Error() string {
	return e.Attempt + " failed: " + e.Category + "/" + e.Code + ": " + e.Message
}

// External failure categories.
const (
	CategoryNotFound  = "notFound"  // 404
	CategoryConflict  = "conflict"  // 409: external version moved; reload
	CategoryTransient = "transient" // 5xx other than ambiguous outcomes
	CategoryAmbiguous = "ambiguous" // outcome unknown (response lost/timeout)
	CategoryRejected  = "rejected"  // 4xx: the service refuses the request
	CategoryUnknown   = "unknown"   // unmapped / malformed response
)

// AsCallError unwraps to *CallError.
func AsCallError(err error) (*CallError, bool) {
	var ce *CallError
	if errors.As(err, &ce) {
		return ce, true
	}
	return nil, false
}

// ExternalClient is the port the reconcile loop uses against the actual
// resource service. The HTTP adapter under internal/adapter implements it;
// tests can substitute a fake.
type ExternalClient interface {
	Observe(ctx context.Context, id string) (*ExternalResource, error)
	Create(ctx context.Context, id, idempotencyKey string, spec ExternalSpec) (*ExternalResource, error)
	Update(ctx context.Context, id string, expectedVersion int64, spec ExternalSpec) (*ExternalResource, error)
	Delete(ctx context.Context, id string) error
}
