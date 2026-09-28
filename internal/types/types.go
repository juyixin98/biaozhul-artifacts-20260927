// Package types defines the data contracts shared by every layer of the
// admission pipeline: resource objects, admission requests/responses,
// JSON-patch operations, structured failures and audit summaries.
//
// Nothing in this package has side effects; it is the wire contract between
// the pipeline, plugins, adapters, storage and the HTTP surface.
package types

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"
)

// Category partitions every failure into a small stable set. Tests assert on
// these categories instead of on error strings, so they are part of the
// contract and must not be renumbered/renamed casually.
type Category string

const (
	// CatNone is used on successful outcomes and clean policy denials.
	CatNone Category = ""
	// CatInvalidInput: malformed request, bad field value, schema failure.
	CatInvalidInput Category = "InvalidInput"
	// CatIllegalMutation: a mutating plugin touched a path outside its declared
	// allowed prefixes, the immutable hard guard, or a path that cannot exist in
	// the current object (e.g. replace of a missing member).
	CatIllegalMutation Category = "IllegalMutation"
	// CatStateConflict: object state forbids the operation (immutable-field
	// change on UPDATE, version conflict).
	CatStateConflict Category = "StateConflict"
	// CatQuotaExhausted: the local quota adapter rejected for lack of capacity.
	CatQuotaExhausted Category = "QuotaExhausted"
	// CatComputeFailure: a plugin failed internally (panic, I/O, bad math,
	// mutation chain that never converges).
	CatComputeFailure Category = "ComputeFailure"
	// CatTimeout: a plugin exceeded its configured deadline. The timeout cause
	// is always distinguishable from a plain compute failure.
	CatTimeout Category = "Timeout"
)

// Retryable reports whether the reconciler may re-attempt the request after
// this category of failure.
func (c Category) Retryable() bool {
	return c == CatQuotaExhausted || c == CatComputeFailure || c == CatTimeout
}

// Phase identifies where in the pipeline a record was produced.
type Phase string

const (
	PhaseDefault    Phase = "default"    // built-in defaulting transformers
	PhaseMutating   Phase = "mutating"   // ordered mutating plugin chain
	PhaseValidating Phase = "validating" // post-mutation validation chain
)

// Metadata is the object identity section. The maps are always present in the
// JSON document (even when empty) so mutators can add members under
// /metadata/labels/... without having to create the parent container.
type Metadata struct {
	Namespace   string            `json:"namespace"`
	Name        string            `json:"name"`
	Labels      map[string]string `json:"labels"`
	Annotations map[string]string `json:"annotations"`
}

// Spec is the workload-shaped resource body. Immutable is the hard-guarded
// field: no mutator may ever write /spec/immutable.
type Spec struct {
	Replicas  *int   `json:"replicas,omitempty"`
	CPU       string `json:"cpu,omitempty"`
	Memory    string `json:"memory,omitempty"`
	Immutable bool   `json:"immutable,omitempty"`
	// Extra holds fixture/projected values, addressable as /spec/extra/<key>.
	// Always present in the document so plugins can project into it.
	Extra map[string]any `json:"extra"`
}

// Object is the admitted resource, intentionally CRD/Kubernetes shaped so that
// patch paths (/spec/replicas, /metadata/labels/team, ...) are meaningful.
type Object struct {
	APIVersion string   `json:"apiVersion"`
	Kind       string   `json:"kind"`
	Metadata   Metadata `json:"metadata"`
	Spec       Spec     `json:"spec"`
}

// Fingerprint returns a stable content hash of the object, used to (a) prove
// whether a re-entrant mutation changed anything, (b) detect non-convergence
// and (c) bind the audit record to the exact final object.
func (o Object) Fingerprint() string {
	b, _ := CanonicalJSON(o)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:16])
}

// Summary is the compact, audit-bound description of an object.
type Summary struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Namespace  string `json:"namespace"`
	Name       string `json:"name"`
	Replicas   int    `json:"replicas"`
	CPU        string `json:"cpu,omitempty"`
	Memory     string `json:"memory,omitempty"`
	Immutable  bool   `json:"immutable,omitempty"`
	// Fingerprint binds the summary to the full object bytes, not just the
	// visible scalar fields.
	Fingerprint string `json:"fingerprint"`
}

// Summarize computes the summary for an object.
func Summarize(o Object) Summary {
	r := 0
	if o.Spec.Replicas != nil {
		r = *o.Spec.Replicas
	}
	return Summary{
		APIVersion:  o.APIVersion,
		Kind:        o.Kind,
		Namespace:   o.Metadata.Namespace,
		Name:        o.Metadata.Name,
		Replicas:    r,
		CPU:         o.Spec.CPU,
		Memory:      o.Spec.Memory,
		Immutable:   o.Spec.Immutable,
		Fingerprint: o.Fingerprint(),
	}
}

// Op is the supported RFC 6902 subset.
type Op string

const (
	OpAdd     Op = "add"     // create or overwrite an object member
	OpReplace Op = "replace" // overwrite an existing member (missing => error)
	OpRemove  Op = "remove"  // remove an existing member (missing => error)
)

// PatchOp is one patch step. Path is an RFC 6901 JSON pointer, e.g.
// "/spec/replicas" or "/metadata/labels/team".
type PatchOp struct {
	Op    Op     `json:"op"`
	Path  string `json:"path"`
	Value any    `json:"value,omitempty"`
}

// Patch is an ordered list of operations emitted by one mutator in one pass.
type Patch struct {
	Ops []PatchOp `json:"ops"`
}

// PluginResult is what a single mutator returns.
type PluginResult struct {
	Name    string
	Mutated bool
	Patch   Patch
	Message string
}

// Step records one executed plugin invocation: its patch, the object fingerprint
// immediately afterwards, and the judgement. These records are what a replay
// reads.
type Step struct {
	Pass      int       `json:"pass"`
	Index     int       `json:"index"`
	Phase     Phase     `json:"phase"`
	Plugin    string    `json:"plugin"`
	Mutated   bool      `json:"mutated,omitempty"`
	Patch     []PatchOp `json:"patch,omitempty"`
	Allowed   *bool     `json:"allowed,omitempty"`
	Tolerated bool      `json:"tolerated,omitempty"` // error swallowed by FailOpen
	Message   string    `json:"message,omitempty"`
	Category  Category  `json:"category,omitempty"`
	StartedAt time.Time `json:"startedAt"`
	ElapsedMS int64     `json:"elapsedMs"`
	// BeforeHash / AfterHash pin the intermediate object state for this step.
	BeforeHash string `json:"beforeHash,omitempty"`
	AfterHash  string `json:"afterHash,omitempty"`
}

// Review is the transport-agnostic admission request.
type Review struct {
	// UID is the caller-provided idempotency key. Re-posting the same UID is a
	// duplicate call: the stored verdict is returned and the chain is not
	// re-run.
	UID       string `json:"uid"`
	Operation string `json:"operation"` // CREATE | UPDATE
	Object    Object `json:"object"`
	// OldObject is present on UPDATE; used by the immutable-field validator.
	OldObject  *Object   `json:"oldObject,omitempty"`
	DryRun     bool      `json:"dryRun,omitempty"`
	ReceivedAt time.Time `json:"receivedAt,omitempty"`
}

// Decision is a validator verdict. A clean denial is not an error: the request
// is well-formed, it simply violates policy.
type Decision struct {
	Allowed bool
	Reason  string
}

// Response is the complete verdict bound to one Review.
type Response struct {
	UID       string `json:"uid"`
	RunID     string `json:"runId"`
	Operation string `json:"operation"`
	DryRun    bool   `json:"dryRun"`
	Allowed   bool   `json:"allowed"`
	// DenyReason is set on a clean policy denial.
	DenyReason      string   `json:"denyReason,omitempty"`
	Object          Object   `json:"object"`
	Final           Summary  `json:"finalSummary"`
	Steps           []Step   `json:"steps"`
	FailureCategory Category `json:"failureCategory,omitempty"`
	FailurePhase    Phase    `json:"failurePhase,omitempty"`
	FailedPlugin    string   `json:"failedPlugin,omitempty"`
	Message         string   `json:"message,omitempty"`
	// Replayed is true when this response was served from the idempotency store
	// instead of running the chain.
	Replayed   bool      `json:"replayed,omitempty"`
	Attempt    int       `json:"attempt"`
	FinishedAt time.Time `json:"finishedAt"`
}

// AuditEvent is persisted and binds the request UID to the final object
// summary, every intermediate patch and the structured outcome.
type AuditEvent struct {
	RunID        string    `json:"runId"`
	UID          string    `json:"uid"`
	Attempt      int       `json:"attempt"`
	Terminal     bool      `json:"terminal"`
	Operation    string    `json:"operation"`
	Kind         string    `json:"kind"`
	DryRun       bool      `json:"dryRun"`
	Allowed      bool      `json:"allowed"`
	Replayed     bool      `json:"replayed"`
	DenyReason   string    `json:"denyReason,omitempty"`
	BeforeHash   string    `json:"beforeHash"`
	AfterHash    string    `json:"afterHash"`
	Final        Summary   `json:"finalSummary"`
	Steps        []Step    `json:"steps"`
	Category     Category  `json:"category,omitempty"`
	FailurePhase Phase     `json:"failurePhase,omitempty"`
	FailedPlugin string    `json:"failedPlugin,omitempty"`
	Message      string    `json:"message,omitempty"`
	StartedAt    time.Time `json:"startedAt"`
	FinishedAt   time.Time `json:"finishedAt"`
}

// CanonicalJSON renders a value as deterministic, whitespace-free JSON. Go's
// encoding/json already emits map keys in sorted order, and all struct fields
// have a fixed order, so the output is stable across processes.
func CanonicalJSON(v any) ([]byte, error) {
	return json.Marshal(v)
}
