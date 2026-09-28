// Package model defines the core resource model shared by every layer:
// desired specs, observed reality, resource identity and the lifecycle kinds.
package model

import "fmt"

// Kind identifies a resource type. The supported set is intentionally small
// (see registry) but the planner is generic over kinds that implement the
// ResourceKind contract.
type Kind string

const (
	KindNetwork   Kind = "Network"
	KindSubnet    Kind = "Subnet"
	KindInstance  Kind = "Instance"
	KindBucket    Kind = "Bucket"
	KindDisk      Kind = "Disk"
)

// Action is the lifecycle operation the planner may issue against a resource.
type Action string

const (
	ActionCreate Action = "create"
	ActionUpdate Action = "update"
	ActionReplace Action = "replace" // delete(reverse order) + create, identity preserved
	ActionDelete Action = "delete"
	ActionNoop   Action = "noop"
)

// State is the reconciliation state of a resource instance.
type State string

const (
	StateExists     State = "exists"
	StateCreating   State = "creating" // create issued, result unknown (response lost)
	StateUpdating   State = "updating"
	StateReplacing  State = "replacing"
	StateDeleting   State = "deleting"
	StateDeleted    State = "deleted"
	StateCreateLost State = "create_lost" // superset of creating: a create may or may not have happened
)

// Spec is a desired resource declaration.
// Attrs is the canonicalizable, comparable attribute bag.
// Protected marks a critical resource: deletion/replacement is refused unless
// the same run carries an explicit protection-release token for its ID.
type Spec struct {
	Kind      Kind              `json:"kind"`
	ID        string            `json:"id"`
	DependsOn []string          `json:"depends_on,omitempty"`
	Attrs     map[string]string `json:"attrs"`
	Protected bool              `json:"protected,omitempty"`
}

// Ref returns the stable identity of a desired spec.
func (s Spec) Ref() Ref { return Ref{Kind: s.Kind, ID: s.ID} }

// Ref is a stable resource reference: kind + logical id. Identity never changes;
// an immutable-field change keeps the same Ref but is executed as a replace.
type Ref struct {
	Kind Kind   `json:"kind"`
	ID   string `json:"id"`
}

func (r Ref) String() string { return string(r.Kind) + "/" + r.ID }

// Resource is an observed (actual) resource in the simulated environment.
type Resource struct {
	Ref       Ref               `json:"ref"`
	State     State             `json:"state"`
	Attrs     map[string]string `json:"attrs"`
	Protected bool              `json:"protected"`
	DependsOn []string          `json:"depends_on,omitempty"`
	// External is true for resources that exist in reality but are not in the
	// desired spec. They are left alone unless adoption is requested.
	External bool `json:"external,omitempty"`
	// ProviderToken is the identity returned by the provider on create. It is
	// the evidence that a create actually committed.
	ProviderToken string `json:"provider_token,omitempty"`
}

// ObservedSet is the result of an observation pass over the environment.
type ObservedSet struct {
	// Run is the monotonically increasing observation run number (evidence id).
	Run int64 `json:"run"`
	// Resources keyed by Ref.String().
	Resources map[string]*Resource `json:"resources"`
}

func (o *ObservedSet) Get(ref Ref) (*Resource, bool) {
	r, ok := o.Resources[ref.String()]
	return r, ok
}

// DesiredSet is the declared target state for a run.
type DesiredSet struct {
	Resources []Spec `json:"resources"`
	// ReleaseProtection explicitly lifts deletion protection for the listed
	// refs within THIS run only. Critical-resource destruction requires it.
	ReleaseProtection []string `json:"release_protection,omitempty"`
}

// ByID indexes desired specs by logical id.
func (d *DesiredSet) ByID() (map[string]Spec, error) {
	m := make(map[string]Spec, len(d.Resources))
	for _, s := range d.Resources {
		if s.ID == "" {
			return nil, fmt.Errorf("desired resource of kind %s has empty id", s.Kind)
		}
		if _, dup := m[s.ID]; dup {
			return nil, fmt.Errorf("duplicate desired resource id %q", s.ID)
		}
		m[s.ID] = s
	}
	return m, nil
}

// ProtectionReleased reports whether the run explicitly released protection
// for the given ref string.
func (d *DesiredSet) ProtectionReleased(refStr string) bool {
	for _, r := range d.ReleaseProtection {
		if r == refStr {
			return true
		}
	}
	return false
}
