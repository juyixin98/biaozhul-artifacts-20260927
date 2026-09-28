// Package core holds the pure, storage-free algorithms of the lifecycle
// controller: admission validation (including ownership-cycle
// prevention), cycle detection and the per-tick reconcile planner.
//
// Nothing here performs I/O. The reconcile package feeds snapshots in
// and applies the produced plan out. This split lets the independent
// test oracle reimplement the same rules from the documented semantics
// and compare, rather than trusting the code under test to grade
// itself.
package core

import (
	"fmt"
	"sort"

	"lifecycle.local/v1/internal/model"
)

// LiveIndex is an immutable-by-convention read view over all currently
// stored resources, keyed by UID and by (namespace, name).
type LiveIndex struct {
	ByUID  map[string]*model.Resource
	ByName map[string]*model.Resource // key: namespace + "\x00" + name
}

// NewLiveIndex builds the index used during one tick / one admission.
func NewLiveIndex(all []*model.Resource) *LiveIndex {
	idx := &LiveIndex{
		ByUID:  make(map[string]*model.Resource, len(all)),
		ByName: make(map[string]*model.Resource, len(all)),
	}
	for _, r := range all {
		idx.ByUID[r.UID] = r
		idx.ByName[NameKey(r.Namespace, r.Name)] = r
	}
	return idx
}

// NameKey is the (namespace, name) map key.
func NameKey(namespace, name string) string { return namespace + "\x00" + name }

// LookupName resolves a same-namespace/name row.
func (x *LiveIndex) LookupName(namespace, name string) (*model.Resource, bool) {
	r, ok := x.ByName[NameKey(namespace, name)]
	return r, ok
}

// ValidateStatic checks fields that do not depend on the live graph.
func ValidateStatic(r *model.Resource) error {
	if r == nil {
		return model.NewError(model.ErrKindInvalid, "resource is nil")
	}
	if r.Namespace == "" || r.Name == "" {
		return model.Errorf(model.ErrKindInvalid, "namespace and name are required (got %q/%q)", r.Namespace, r.Name)
	}
	if r.UID == "" {
		return model.Errorf(model.ErrKindInvalid, "uid is required for %s", r.QualifiedName())
	}
	if r.Kind == "" || r.APIVersion == "" {
		return model.Errorf(model.ErrKindInvalid, "kind and apiVersion are required for %s", r.QualifiedName())
	}
	seen := map[string]bool{}
	for _, ref := range r.OwnerRefs {
		if ref.UID == "" || ref.Name == "" || ref.Kind == "" || ref.APIVersion == "" {
			return model.Errorf(model.ErrKindInvalid,
				"%s has an ownerRef missing required fields (uid/name/kind/apiVersion)", r.QualifiedName())
		}
		if ref.Namespace != r.Namespace {
			return model.Errorf(model.ErrKindInvalid,
				"%s ownerRef to %s/%s is cross-namespace; only same-namespace owners are supported",
				r.QualifiedName(), ref.Namespace, ref.Name)
		}
		if seen[ref.UID] {
			return model.Errorf(model.ErrKindInvalid,
				"%s has a duplicate ownerRef uid %s", r.QualifiedName(), ref.UID)
		}
		seen[ref.UID] = true
	}
	return nil
}

// ValidateDeletionPolicy accepts the three policy spellings. Empty
// means the caller will apply the Background default.
func ValidateDeletionPolicy(p string) error {
	switch p {
	case "", model.PolicyForeground, model.PolicyBackground, model.PolicyOrphan:
		return nil
	default:
		return model.Errorf(model.ErrKindInvalidPolicy,
			"deletionPropagationPolicy %q is not one of Foreground|Background|Orphan", p)
	}
}

// AdmitCreate validates a resource creation against the live graph:
// every declared owner reference must resolve BY UID (a same-name row
// with a different UID is a hard OwnerUIDMismatch, never a silent
// re-parent), a referenced owner must not already be deleting, and the
// resulting graph must remain acyclic.
func AdmitCreate(r *model.Resource, idx *LiveIndex) error {
	if err := ValidateStatic(r); err != nil {
		return err
	}
	if _, clash := idx.LookupName(r.Namespace, r.Name); clash {
		return model.Errorf(model.ErrKindAlreadyExists,
			"resource %s/%s already exists", r.Namespace, r.Name)
	}
	if _, clash := idx.ByUID[r.UID]; clash {
		return model.Errorf(model.ErrKindAlreadyExists, "uid %s already in use", r.UID)
	}
	for _, ref := range r.OwnerRefs {
		if err := admitRefTarget(r, ref, idx); err != nil {
			return err
		}
	}
	return admitNoCycle(r.UID, r.OwnerRefs, nil, idx)
}

// AdmitAttach validates adding a single owner reference to an EXISTING
// resource. The same UID-pinning rules apply as on create.
func AdmitAttach(target *model.Resource, ref model.OwnerRef, idx *LiveIndex) error {
	if ref.UID == "" || ref.Name == "" || ref.Kind == "" || ref.APIVersion == "" {
		return model.NewError(model.ErrKindInvalid,
			"ownerRef missing required fields (uid/name/kind/apiVersion)")
	}
	if ref.Namespace == "" {
		ref.Namespace = target.Namespace
	}
	if ref.Namespace != target.Namespace {
		return model.Errorf(model.ErrKindInvalid,
			"cannot attach cross-namespace ownerRef %s/%s to %s",
			ref.Namespace, ref.Name, target.QualifiedName())
	}
	for _, existing := range target.OwnerRefs {
		if existing.UID == ref.UID {
			return model.Errorf(model.ErrKindRefAlreadyExists,
				"ownerRef %s already present on %s", ref.UID, target.QualifiedName())
		}
	}
	if err := admitRefTarget(target, ref, idx); err != nil {
		return err
	}
	return admitNoCycle(target.UID, []model.OwnerRef{ref}, target.OwnerRefs, idx)
}

func admitRefTarget(r *model.Resource, ref model.OwnerRef, idx *LiveIndex) error {
	if ref.UID == r.UID {
		return model.Errorf(model.ErrKindOwnershipCycle,
			"%s cannot own itself (self loop)", r.QualifiedName())
	}
	byName, hasName := idx.LookupName(ref.Namespace, ref.Name)
	byUID, hasUID := idx.ByUID[ref.UID]
	if !hasUID {
		if hasName {
			// Same-name live row but a different incarnation: this is
			// the classic "old ref, new object after recreate" trap.
			return model.Errorf(model.ErrKindUIDMismatch,
				"ownerRef on %s pins uid %s for %s/%s but the live object there is uid %s; refusing to re-parent onto the recreated name",
				r.QualifiedName(), ref.UID, ref.Namespace, ref.Name, byName.UID)
		}
		return model.Errorf(model.ErrKindOwnerNotFound,
			"ownerRef on %s points to uid %s (%s/%s) which does not exist",
			r.QualifiedName(), ref.UID, ref.Namespace, ref.Name)
	}
	// uid resolves; the (name,namespace) tuple must match the pinned
	// incarnation, otherwise the ref is corrupt/stale.
	if byUID.Namespace != ref.Namespace || byUID.Name != ref.Name {
		return model.Errorf(model.ErrKindUIDMismatch,
			"ownerRef on %s calls uid %s %q but that uid is %s; name tuple must match the pinned incarnation",
			r.QualifiedName(), ref.UID, ref.Name, byUID.QualifiedName())
	}
	if byUID.IsDeleting() {
		return model.Errorf(model.ErrKindOwnerDeleting,
			"ownerRef on %s points to %s which is already deleting (policy %s)",
			r.QualifiedName(), ref.UID, byUID.QualifiedName(), byUID.Policy())
	}
	return nil
}

// admitNoCycle verifies that adding newRefs to ownerUID cannot close a
// cycle. Edges point dependent -> owner; a cycle exists if any owner is
// already (transitively) a dependent of ownerUID.
func admitNoCycle(ownerUID string, newRefs []model.OwnerRef, existingRefs []model.OwnerRef, idx *LiveIndex) error {
	// adjDependents[o] = resources that currently point at o.
	adjDependents := map[string][]string{}
	for _, r := range idx.ByUID {
		for _, ref := range r.OwnerRefs {
			adjDependents[ref.UID] = append(adjDependents[ref.UID], r.UID)
		}
	}
	for _, ref := range existingRefs {
		adjDependents[ref.UID] = append(adjDependents[ref.UID], ownerUID)
	}
	// The prospective new edges are checked as if already present.
	startSet := map[string]bool{}
	for _, ref := range newRefs {
		startSet[ref.UID] = true
	}
	for start := range startSet {
		if path := reachAny(adjDependents, start, map[string]bool{ownerUID: true}); path != nil {
			return model.Errorf(model.ErrKindOwnershipCycle,
				"attaching would create an ownership cycle: %s", formatCycle(path, start))
		}
	}
	return nil
}

// reachAny performs DFS from start over edges node -> its dependents and
// returns the node path (start ... target) if any target is reached.
func reachAny(adj map[string][]string, start string, targets map[string]bool) []string {
	if targets[start] {
		return []string{start}
	}
	color := map[string]int{} // 0 white, 1 gray, 2 black
	var dfs func(n string, trail []string) []string
	dfs = func(n string, trail []string) []string {
		color[n] = 1
		next := append(append([]string{}, trail...), n)
		for _, m := range adj[n] {
			if targets[m] {
				return append(next, m)
			}
			if color[m] == 0 {
				if p := dfs(m, next); p != nil {
					return p
				}
			}
		}
		color[n] = 2
		return nil
	}
	return dfs(start, nil)
}

// formatCycle renders a path [start, ..., closingOwner] as
// closingOwner -> start -> ... -> closingOwner.
func formatCycle(path []string, start string) string {
	if len(path) == 0 {
		return start
	}
	closingOwner := path[len(path)-1]
	chain := append([]string{closingOwner}, path...)
	chain = append(chain, closingOwner)
	s := ""
	for i, u := range chain {
		if i > 0 {
			s += " -> "
		}
		s += u
	}
	return s
}

// SortedUIDs returns map keys sorted (used for deterministic breaks and
// logging).
func SortedUIDs(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
