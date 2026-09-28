// Package model defines the resource model shared by the desired-state API,
// the reconciler and the actual resource service.
//
// The three version concepts are deliberately kept separate:
//   - resourceVersion: optimistic-concurrency token on the desired-state
//     record. It changes on every mutating write, including status writes.
//   - generation: incremented by the desired-state API whenever the spec
//     (template) changes. The reconciler must never overwrite a generation it
//     has not yet processed.
//   - observedGeneration: last generation the controller was able to confirm
//     as applied on the actual resource. It may legitimately lag behind
//     generation during convergence and must never move backwards.
package model

import "time"

// FinalizerController is the single finalizer this controller owns. While it
// is present the desired-state API refuses to physically delete a record.
const FinalizerController = "crcontroller.local/external-resource"

// ConditionType is the restricted vocabulary of object conditions.
type ConditionType string

const (
	// ConditionFinalizersPresent records whether the object is protected.
	ConditionFinalizersPresent ConditionType = "FinalizersPresent"
	// ConditionExternalReady reflects the last observed state of the external
	// resource.
	ConditionExternalReady ConditionType = "ExternalReady"
)

// ConditionStatus is True/False/Unknown.
type ConditionStatus string

const (
	CondTrue    ConditionStatus = "True"
	CondFalse   ConditionStatus = "False"
	CondUnknown ConditionStatus = "Unknown"
)

// Condition is one entry of an object's status.conditions list.
type Condition struct {
	Type               ConditionType   `json:"type"`
	Status             ConditionStatus `json:"status"`
	Reason             string          `json:"reason"`
	Message            string          `json:"message"`
	ObservedGeneration int64           `json:"observedGeneration"`
	LastTransition     time.Time       `json:"lastTransitionTime"`
}

// Status is the controller-owned subresource.
type Status struct {
	ObservedGeneration int64       `json:"observedGeneration"`
	ExternalID         string      `json:"externalID,omitempty"`
	State              string      `json:"state,omitempty"`
	Conditions         []Condition `json:"conditions"`
}

// Object is one custom resource on the desired-state plane.
//
// Meta fields are first class, spec is an opaque map. Fields named
// "secret"/"secretRef"/"token"/"password"/"credential" anywhere inside spec
// are considered sensitive and redacted by logx before they are logged.
type Object struct {
	UID         string            `json:"uid"`
	Name        string            `json:"name"`
	Namespace   string            `json:"namespace"`
	Generation  int64             `json:"generation"`
	ResourceVer int64             `json:"resourceVersion"`
	Spec        map[string]any    `json:"spec"`
	SpecHash    string            `json:"specHash"`
	Finalizers  []string          `json:"finalizers"`
	DeletionTS  *time.Time        `json:"deletionTimestamp,omitempty"`
	Status      Status            `json:"status"`
	CreatedAt   time.Time         `json:"createdAt"`
	UpdatedAt   time.Time         `json:"updatedAt"`
	Annotations map[string]string `json:"annotations,omitempty"`
}

// NamespacedName joins namespace and name.
func (o *Object) NamespacedName() string { return o.Namespace + "/" + o.Name }

// HasFinalizer reports whether the controller finalizer is attached.
func (o *Object) HasFinalizer() bool {
	for _, f := range o.Finalizers {
		if f == FinalizerController {
			return true
		}
	}
	return false
}

// Terminating reports that a user deletion was requested but external cleanup
// is still outstanding.
func (o *Object) Terminating() bool { return o.DeletionTS != nil }

// ActualResource is the physical resource owned by the actual-resource
// service. OwnerUID ties it back to exactly one desired object so that a
// lost create response can be claimed instead of recreated.
type ActualResource struct {
	ID         string         `json:"id"`
	OwnerUID   string         `json:"ownerUID"`
	Generation int64          `json:"generation"`
	SpecHash   string         `json:"specHash"`
	Spec       map[string]any `json:"spec"`
	Version    int64          `json:"version"`
	State      string         `json:"state"`
	CreatedAt  time.Time      `json:"createdAt"`
	UpdatedAt  time.Time      `json:"updatedAt"`
}
