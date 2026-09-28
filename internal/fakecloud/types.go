// Package fakecloud is the independent "actual resource service".
//
// It is deliberately self-contained: it imports none of the controller's
// internal packages and keeps its state in memory, so it can model an
// external cloud API that the controller does not own. Its behaviour is
// idempotent-key aware and optimistic-concurrency based, and the
// /internal/faults endpoint lets a test inject failures at the exact
// interaction (create response lost, delete failure, stale GET, ...).
package fakecloud

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sync"
	"time"
)

// Spec is the desired-state shape understood by the actual service. It mirrors
// the controller's WidgetSpec but is defined locally on purpose: the
// fingerprint the test oracle compares against is produced independently.
type Spec struct {
	Replicas    int    `json:"replicas"`
	Color       string `json:"color"`
	SecretToken string `json:"secretToken,omitempty"`
}

// Resource is a resource instance inside the actual service.
type Resource struct {
	ID             string    `json:"id"`
	Name           string    `json:"name"`
	IdempotencyKey string    `json:"idempotencyKey"`
	Version        int64     `json:"version"`
	Spec           Spec      `json:"spec"`
	SpecFP         string    `json:"specFingerprint"`
	CreatedAt      time.Time `json:"createdAt"`
	UpdatedAt      time.Time `json:"updatedAt"`
}

// Fingerprint is the service-side canonical hash of a spec.
func Fingerprint(s Spec) string {
	b, err := json.Marshal(s)
	if err != nil {
		panic(err)
	}
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// store is the in-memory resource database.
type store struct {
	mu   sync.Mutex
	byID map[string]*Resource
}

func newStore() *store { return &store{byID: map[string]*Resource{}} }

// Fault kinds understood by /internal/faults.
const (
	FaultCreateResponseLost = "createResponseLost" // commit succeeds, client gets 500
	FaultCreateFail         = "createFail"         // commit fails, 503
	FaultDeleteFail         = "deleteFail"         // 503, resource survives
	FaultStaleGet           = "staleGet"           // GET returns the pre-update snapshot
	FaultGetFail            = "getFail"            // 503
	FaultSlow               = "slow"               // delay, succeeds
)

// fault describes an injected failure for one operation.
type fault struct {
	Kind      string `json:"kind"`
	Remaining int    `json:"remaining"` // times it should still fire
	DelayMS   int    `json:"delayMs,omitempty"`
	// snapshot holds the resource state captured before the most recent
	// successful update while a staleGet fault was armed. GET returns this
	// once while remaining > 0, simulating a read replica lagging behind.
	snapshot *Resource
}

// FaultConfig is the request body for PUT /internal/faults/{op}.
type FaultConfig struct {
	Kind    string `json:"kind"`
	Times   int    `json:"times"`
	DelayMS int    `json:"delayMs,omitempty"`
}

// FaultStatus reports armed faults and resource counts.
type FaultStatus struct {
	Armed         map[string]string `json:"armed"`
	ResourceCount int               `json:"resourceCount"`
}

func (s *store) clone(r *Resource) *Resource {
	cp := *r
	return &cp
}
