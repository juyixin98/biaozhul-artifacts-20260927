// Package model defines the resource model and the data contracts exchanged
// between the HTTP adapter, the admission pipeline (mutators + validators),
// the reconciler, storage and the audit log.
//
// Every object flows through the system as a generic document
// (map[string]any). Typed helpers (Resource, Spec) exist only for envelope
// access and for computing stable summaries; the pipeline itself never depends
// on concrete resource fields beyond the envelope.
package model

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
)

// Operation is the verb of an admission request.
type Operation string

const (
	OpCreate Operation = "CREATE"
	OpUpdate Operation = "UPDATE"
	OpDelete Operation = "DELETE"
)

// Valid reports whether o is a supported operation.
func (o Operation) Valid() bool {
	return o == OpCreate || o == OpUpdate || o == OpDelete
}

// Metadata is the metadata stanza every resource carries.
type Metadata struct {
	Name        string            `json:"name"`
	Namespace   string            `json:"namespace,omitempty"`
	Labels      map[string]string `json:"labels,omitempty"`
	Annotations map[string]string `json:"annotations,omitempty"`
}

// Resource is the typed view of an envelope document. It is used for input
// validation and summary computation; pipeline plugins manipulate the raw map.
type Resource struct {
	APIVersion string         `json:"apiVersion"`
	Kind       string         `json:"kind"`
	Metadata   Metadata       `json:"metadata"`
	Spec       map[string]any `json:"spec,omitempty"`
}

// ResourceFromMap converts a raw document into the typed envelope view.
func ResourceFromMap(doc map[string]any) (Resource, error) {
	b, err := json.Marshal(doc)
	if err != nil {
		return Resource{}, fmt.Errorf("re-marshal document: %w", err)
	}
	var r Resource
	if err := json.Unmarshal(b, &r); err != nil {
		return Resource{}, fmt.Errorf("resource envelope: %w", err)
	}
	return r, nil
}

// Canonical renders a document deterministically. JSON numbers are emitted
// without Go-map float artifacts: all numeric spec values produced by this
// codebase are integers, which we normalize through the json encoder, and map
// keys are sorted by encoding/json.
func Canonical(doc map[string]any) ([]byte, error) {
	b, err := json.Marshal(normalize(doc))
	if err != nil {
		return nil, err
	}
	return b, nil
}

// normalize turns float64 integer values into int64 so canonical JSON does not
// leak "100" as "100.0". It is applied recursively.
func normalize(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			out[k] = normalize(val)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i := range t {
			out[i] = normalize(t[i])
		}
		return out
	case float64:
		if t == float64(int64(t)) {
			return int64(t)
		}
		return t
	default:
		return v
	}
}

// Summary is the final-object fingerprint bound to both the admission response
// and the audit record. It lets a downstream party prove *which* object a
// decision was made for.
type Summary struct {
	Kind      string `json:"kind"`
	Name      string `json:"name"`
	Namespace string `json:"namespace,omitempty"`
	Replicas  int64  `json:"replicas,omitempty"`
	Digest    string `json:"digest"` // sha256 of canonical JSON
}

// Summarize produces a stable digest plus a few human-identifiable fields.
func Summarize(doc map[string]any) (Summary, error) {
	r, err := ResourceFromMap(doc)
	if err != nil {
		return Summary{}, err
	}
	b, err := Canonical(doc)
	if err != nil {
		return Summary{}, err
	}
	sum := sha256.Sum256(b)
	s := Summary{
		Kind:      r.Kind,
		Name:      r.Metadata.Name,
		Namespace: r.Metadata.Namespace,
		Digest:    hex.EncodeToString(sum[:]),
	}
	if v, ok := r.Spec["replicas"]; ok {
		if n, ok := toInt(v); ok {
			s.Replicas = n
		}
	}
	return s, nil
}

func toInt(v any) (int64, bool) {
	switch n := v.(type) {
	case int:
		return int64(n), true
	case int64:
		return n, true
	case float64:
		if n == float64(int64(n)) {
			return int64(n), true
		}
	}
	return 0, false
}

// Phase labels where in the pipeline a step ran.
type Phase string

const (
	PhaseMutate   Phase = "mutate"
	PhaseValidate Phase = "validate"
	PhaseCommit   Phase = "commit"
)

// PatchOp is one RFC6902-ish operation emitted by a mutator.
type PatchOp struct {
	Op    string `json:"op"`              // add | replace | remove
	Path  string `json:"path"`            // JSON pointer, e.g. /spec/replicas
	Value any    `json:"value,omitempty"` // absent for remove
}

// Step records one plugin invocation: inputs, outputs and the judgement. It is
// the unit of replay: the audit log stores one Step record per invocation so a
// reviewer can reconstruct every intermediate patch.
type Step struct {
	Order      int       `json:"order"`
	Phase      Phase     `json:"phase"`
	Plugin     string    `json:"plugin"`
	Patches    []PatchOp `json:"patches,omitempty"`
	Changed    bool      `json:"changed"`
	DurationMS int64     `json:"durationMs"`
	Decision   string    `json:"decision"` // applied | skipped-open | denied | error
	Reason     Reason    `json:"reason,omitempty"`
	Detail     string    `json:"detail,omitempty"`
}

// Request is an admission request (the inbound contract).
type Request struct {
	UID       string         `json:"uid"`
	Operation Operation      `json:"operation"`
	Object    map[string]any `json:"object,omitempty"`
	OldObject map[string]any `json:"oldObject,omitempty"`
}

// Validate performs request-level (pre-pipeline) input validation. These
// failures are always synchronous and category ReasonInvalidInput.
func (r Request) Validate() error {
	if r.UID == "" {
		return errors.New("uid is required")
	}
	if !r.Operation.Valid() {
		return fmt.Errorf("operation %q is not one of CREATE/UPDATE/DELETE", r.Operation)
	}
	if r.Operation == OpDelete {
		if len(r.Object) == 0 && len(r.OldObject) == 0 {
			return errors.New("delete requires object or oldObject identity")
		}
		return nil
	}
	if len(r.Object) == 0 {
		return errors.New("object is required for " + string(r.Operation))
	}
	res, err := ResourceFromMap(r.Object)
	if err != nil {
		return err
	}
	if res.APIVersion == "" {
		return errors.New("object.apiVersion is required")
	}
	if res.Kind == "" {
		return errors.New("object.kind is required")
	}
	if res.Metadata.Name == "" {
		return errors.New("object.metadata.name is required")
	}
	return nil
}

// Decision is the overall verdict for a request.
type Decision string

const (
	DecisionAllowed Decision = "allowed"
	DecisionDenied  Decision = "denied"
)

// Reason is the machine-readable failure category. The task requires input
// errors, state conflicts, resource exhaustion and compute failures to be
// distinguishable; timeout is its own category.
type Reason string

const (
	ReasonOK                Reason = ""
	ReasonInvalidInput      Reason = "invalid_input"
	ReasonIllegalPath       Reason = "illegal_path"
	ReasonTimeout           Reason = "timeout"
	ReasonComputeFailure    Reason = "compute_failure"
	ReasonValidationDenied  Reason = "validation_denied"
	ReasonResourceExhausted Reason = "resource_exhausted"
	ReasonStateConflict     Reason = "state_conflict"
)

// Category groups reasons into the four broad classes required by the spec.
func (r Reason) Category() string {
	switch r {
	case ReasonOK:
		return "ok"
	case ReasonInvalidInput:
		return "input_error"
	case ReasonStateConflict:
		return "state_conflict"
	case ReasonResourceExhausted:
		return "resource_exhausted"
	case ReasonTimeout, ReasonComputeFailure, ReasonIllegalPath, ReasonValidationDenied:
		return "compute_failure"
	default:
		return "unknown"
	}
}

// HTTPStatus maps a reason onto the status the adapter returns. Allowed maps to
// 200; input problems to 400; conflicts/quota to 409/429; compute problems to
// 500 and timeouts to 504.
func (r Reason) HTTPStatus() int {
	switch r {
	case ReasonOK:
		return 200
	case ReasonInvalidInput:
		return 400
	case ReasonValidationDenied:
		return 422
	case ReasonResourceExhausted:
		return 429
	case ReasonStateConflict:
		return 409
	case ReasonTimeout:
		return 504
	case ReasonComputeFailure, ReasonIllegalPath:
		return 500
	default:
		return 500
	}
}

// Response is the outbound contract. A denied response still carries every
// step and, when an object was produced, the final object and its summary.
type Response struct {
	UID          string         `json:"uid"`
	Decision     Decision       `json:"decision"`
	Reason       Reason         `json:"reason,omitempty"`
	Message      string         `json:"message,omitempty"`
	FinalObject  map[string]any `json:"finalObject,omitempty"`
	FinalSummary *Summary       `json:"finalSummary,omitempty"`
	Steps        []Step         `json:"steps"`
	Patches      []PatchOp      `json:"patches,omitempty"`
	DurationMS   int64          `json:"durationMs"`
}
