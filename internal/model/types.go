// Package model holds the domain objects for the custom resource controller.
//
// A Widget is the synthetic custom resource. The desired state lives in Spec;
// the controller-owned bookkeeping lives in Status. The distinction that drives
// the whole reconciliation design is:
//
//   - Metadata.Generation        : desired-state generation, bumped whenever the
//     user changes the spec.
//   - Status.ObservedGeneration  : newest generation the controller has observed
//     and made a decision about.
//   - Status.ReconciledGeneration: newest generation confirmed to be applied in
//     the external resource service. It must never
//     move forward on a stale or ambiguous result.
package model

import "time"

// WidgetSpec is the user-controlled desired state. SecretToken is synthetic
// sensitive data: it is persisted locally and forwarded to the local fake
// service, but it must never appear in logs (see internal/diag).
type WidgetSpec struct {
	Replicas    int    `json:"replicas"`
	Color       string `json:"color"`
	SecretToken string `json:"secretToken,omitempty"`
}

// Condition mirrors the familiar Kubernetes-style condition shape.
type Condition struct {
	Type               string    `json:"type"`
	Status             string    `json:"status"`
	Reason             string    `json:"reason,omitempty"`
	Message            string    `json:"message,omitempty"`
	ObservedGeneration int64     `json:"observedGeneration"`
	LastTransitionTime time.Time `json:"lastTransitionTime"`
}

// Attempt records the controller's most recent decision for a resource so that
// diagnostics can explain why a step was accepted, rejected or left undecided.
type Attempt struct {
	// Phase is the external interaction: create | update | delete | observe.
	Phase string `json:"phase"`
	// Decision is accepted | rejected | undecidable.
	Decision string `json:"decision"`
	// Action describes what the controller did, e.g. create, claim, noop,
	// refuse-overwrite, wait.
	Action     string    `json:"action"`
	Reason     string    `json:"reason"`
	Category   string    `json:"category,omitempty"`
	RequestID  string    `json:"requestId,omitempty"`
	ExternalID string    `json:"externalId,omitempty"`
	At         time.Time `json:"at"`
}

// Phase values for WidgetStatus.Phase.
const (
	PhasePending  = "Pending"
	PhaseSyncing  = "Syncing"
	PhaseReady    = "Ready"
	PhaseDeleting = "Deleting"
	PhaseError    = "Error"
)

// WidgetStatus is controller-owned state.
type WidgetStatus struct {
	ObservedGeneration   int64       `json:"observedGeneration"`
	ReconciledGeneration int64       `json:"reconciledGeneration"`
	ExternalID           string      `json:"externalId,omitempty"`
	ExternalVersion      int64       `json:"externalVersion,omitempty"`
	Phase                string      `json:"phase"`
	Conditions           []Condition `json:"conditions,omitempty"`
	LastAttempt          *Attempt    `json:"lastAttempt,omitempty"`
}

// ObjectMeta carries identity and concurrency bookkeeping.
type ObjectMeta struct {
	Name              string     `json:"name"`
	UID               string     `json:"uid"`
	Generation        int64      `json:"generation"`
	ResourceVersion   int64      `json:"resourceVersion"`
	DeletionTimestamp *time.Time `json:"deletionTimestamp,omitempty"`
	Finalizers        []string   `json:"finalizers,omitempty"`
	CreatedAt         time.Time  `json:"createdAt"`
	UpdatedAt         time.Time  `json:"updatedAt"`
}

// Widget is the custom resource persisted in SQLite.
type Widget struct {
	Meta   ObjectMeta   `json:"metadata"`
	Spec   WidgetSpec   `json:"spec"`
	Status WidgetStatus `json:"status"`
}
