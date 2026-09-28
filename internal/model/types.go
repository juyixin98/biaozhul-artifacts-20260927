// Package model contains the storage-neutral domain types of the local
// resource lifecycle controller: resources, owner references, GC audit
// events and the canonical error taxonomy.
//
// The model intentionally mirrors the subset of the Kubernetes object
// lifecycle that this exercise targets (deletionPropagation, UID-based
// ownerReferences, finalizers), but every type here is owned by this
// project.
package model

import (
	"fmt"
	"time"
)

// Deletion policy constants. These are the three supported values of
// deletionPropagationPolicy (foreground / background / orphan).
const (
	PolicyForeground = "Foreground"
	PolicyBackground = "Background"
	PolicyOrphan     = "Orphan"
)

// Reserved controller finalizers. User finalizers registered through the
// finalizer registry use non-reserved keys.
const (
	// FinalizerDeletionCohort marks a resource deleted in foreground mode.
	// While present, the resource is only a "deleting placeholder": its
	// body is retained so blocking dependents can still resolve their
	// owner reference, and it is physically deleted only after every
	// blocking dependent is gone (or an ownership cycle was diagnosed and
	// broken).
	FinalizerDeletionCohort = "lifecycle.local/fg-deletion"
	// FinalizerOrphanDependents implements the Orphan propagation policy.
	// When the owner is deleted, the reconciler strips this resource's UID
	// from every dependent's ownerReferences before the owner is removed.
	FinalizerOrphanDependents = "lifecycle.local/orphan-dependents"

	// ReservedPrefix prevents user finalizers from shadowing controller
	// finalizers.
	ReservedPrefix = "lifecycle.local/"
)

// OwnerRef is one edge in the ownership graph. Identity is the tuple
// (namespace, name, UID): name alone can be reused after deletion, so
// every reference pins the owner's UID. BlockOwnerDeletion only has an
// effect for foreground deletions.
type OwnerRef struct {
	APIVersion         string `json:"apiVersion"`
	Kind               string `json:"kind"`
	Namespace          string `json:"namespace"`
	Name               string `json:"name"`
	UID                string `json:"uid"`
	BlockOwnerDeletion bool   `json:"blockOwnerDeletion,omitempty"`
}

// Key returns the identity tuple used for equality/dedup of references.
// UID is part of the key, so a same-name recreated owner never collides
// with a reference that pinned the previous incarnation.
func (r OwnerRef) Key() string { return r.UID }

// Resource is the persisted aggregate: spec fields plus lifecycle state.
type Resource struct {
	Namespace  string     `json:"namespace"`
	Name       string     `json:"name"`
	UID        string     `json:"uid"`
	Kind       string     `json:"kind"`
	APIVersion string     `json:"apiVersion"`
	Spec       []byte     `json:"spec,omitempty"`
	OwnerRefs  []OwnerRef `json:"ownerRefs,omitempty"`
	Finalizers []string   `json:"finalizers,omitempty"`

	CreationTimestamp time.Time  `json:"creationTimestamp"`
	DeletionTimestamp *time.Time `json:"deletionTimestamp,omitempty"`
	// DeletionPolicy records the propagation policy of the DELETE that
	// set DeletionTimestamp (empty when not deleting).
	DeletionPolicy string `json:"deletionPolicy,omitempty"`

	ResourceVersion int64       `json:"resourceVersion"`
	Generation      int64       `json:"generation"`
	Conditions      []Condition `json:"conditions,omitempty"`
}

// QualifiedName is the per-namespace unique name of an incarnation.
func (r *Resource) QualifiedName() string { return r.Namespace + "/" + r.Name }

// IsDeleting reports whether a deletion has been requested.
func (r *Resource) IsDeleting() bool { return r.DeletionTimestamp != nil }

// Policy returns the effective propagation policy for a deleting
// resource, defaulting to Background per the documented semantics.
func (r *Resource) Policy() string {
	switch r.DeletionPolicy {
	case PolicyForeground, PolicyBackground, PolicyOrphan:
		return r.DeletionPolicy
	default:
		return PolicyBackground
	}
}

// HasFinalizer is the membership test used by every layer.
func (r *Resource) HasFinalizer(key string) bool {
	for _, f := range r.Finalizers {
		if f == key {
			return true
		}
	}
	return false
}

func (r *Resource) String() string {
	return fmt.Sprintf("%s(%s)", r.QualifiedName(), r.UID)
}

// Condition types.
const (
	ConditionTerminating = "Terminating"
	ConditionOrphaning   = "Orphaning"
	ConditionFinalizerFailure = "FinalizerFailure"
	ConditionCycle       = "CycleDetected"
)

// Canonical condition reasons so tests can assert a concrete failure
// category instead of grepping messages.
const (
	ReasonFinalizerFailed = "FinalizerFailed"
	ReasonFinalizerPanic  = "FinalizerPanicked"
	ReasonUnknownFinalizer = "UnknownFinalizer"
	ReasonCycleDetected   = "OwnershipCycle"
	ReasonInvariantBroken = "InvariantBroken"
	ReasonOrphanStripping = "OrphanRefsStripped"
	ReasonDeletionBlocked = "WaitingForDependents"
)

// Condition is a typed status entry surfaced on the resource and stored
// separately for historical querying.
type Condition struct {
	Type               string    `json:"type"`
	Status             string    `json:"status"` // True / False
	Reason             string    `json:"reason,omitempty"`
	Message            string    `json:"message,omitempty"`
	ObservedGeneration int64     `json:"observedGeneration"`
	LastTransition     time.Time `json:"lastTransitionTime"`
}

const (
	ConditionTrue  = "True"
	ConditionFalse = "False"
)
