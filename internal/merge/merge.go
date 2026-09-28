// Package merge implements the declarative field-level three-way merge.
//
// The merge is pure: given live state, an ownership table, the applying
// manager's previously applied config, the new config and a schema, it returns
// the next live state and updated ownership — or a structured state_conflict
// listing every blocking path and its current owners. No I/O happens here;
// storage and serialization live in other packages.
package merge

import (
	"encoding/json"
	"fmt"
	"math/big"
	"sort"

	"fieldapply/internal/model"
)

// Inputs bundles a single apply against one resource.
type Inputs struct {
	ResourceID string
	Manager    string
	Live       any
	Owners     model.Owners
	Previous   any // manager's last applied config; nil on first apply
	Config     any // new desired config
	Force      bool
	Schema     model.Schema
	BaseRev    int64 // <=0 skips the optimistic-concurrency check
	Rev        int64 // current revision
}

// Result is the outcome of a successful merge.
type Result struct {
	Live      any
	Owners    model.Owners
	Applied   any
	Changed   bool
	Changes   model.ChangeSet
	NewOwners []string // paths the manager did not own before this apply
}

type engine struct {
	in          *Inputs
	live        any
	owners      model.Owners
	before      map[string]struct{} // paths owned by the manager when Apply started
	conflicts   []model.Conflict
	changed     bool // a live value changed or a field was removed
	ownersMoved bool // ownership-only change (a share was added/stripped)
}

// Apply executes the three-way merge. On conflict the returned error is a
// *model.Error with category state_conflict and one Conflict per blocking
// path; inputs are never mutated.
func Apply(in *Inputs) (*Result, error) {
	if in.Manager == "" {
		return nil, fail(model.CatInvalidInput, "manager_required", "field manager must not be empty")
	}
	if in.Schema.Lists == nil {
		in.Schema = model.Schema{Lists: map[string]model.ListKind{}, Keys: in.Schema.Keys}
	}
	if in.Config == nil {
		return nil, fail(model.CatInvalidInput, "config_required", "applied config must not be null")
	}
	cfg, ok := in.Config.(map[string]any)
	if !ok {
		return nil, fail(model.CatInvalidInput, "config_not_object", "applied config must be a JSON object")
	}
	// Explicit null handling (matching server-side-apply): a null member is
	// pruned from the desired object before merging. Pruning releases the
	// manager's ownership share exactly as omitting the field would: a field
	// the manager solely owns is deleted, a co-owned field survives, and a
	// null on a field the manager never owned can never delete someone
	// else's value. "Uncommitted" (a live value nobody applied) is therefore
	// distinct from "explicitly deleted" (sole owner omits/prunes it).
	cfg = stripNulls(cfg).(map[string]any)
	if err := validateValue(cfg, model.Path{}, &in.Schema); err != nil {
		return nil, err
	}
	if err := in.Schema.Validate(cfg); err != nil {
		return nil, &model.Error{Category: model.CatInvalidInput, Code: "schema_invalid", Message: err.Error()}
	}
	if in.BaseRev > 0 && in.BaseRev != in.Rev {
		return nil, fail(model.CatStateConflict, "revision_stale",
			fmt.Sprintf("expected revision %d but resource is at %d", in.BaseRev, in.Rev))
	}

	before := map[string]struct{}{}
	for ps, ms := range in.Owners {
		if _, mine := ms[in.Manager]; mine {
			before[ps] = struct{}{}
		}
	}

	live := cloneValue(in.Live)
	if live == nil {
		live = map[string]any{}
	}
	e := &engine{
		in:     in,
		live:   live,
		owners: in.Owners.Clone(),
		before: before,
	}

	next := e.reconcileObject(model.Path{}, live.(map[string]any), cfg, true)

	if len(e.conflicts) > 0 {
		sort.SliceStable(e.conflicts, func(i, j int) bool {
			return e.conflicts[i].Path < e.conflicts[j].Path
		})
		return nil, &model.Error{
			Category:  model.CatStateConflict,
			Code:      "field_conflict",
			Message:   "one or more fields are owned by other managers; retry with force=true to take them over",
			Conflicts: e.conflicts,
		}
	}

	changed := e.changed || e.ownersMoved
	if !e.changed && in.Previous != nil {
		changed = changed || !EqualJSON(in.Previous, cfg)
	}

	newOwners := []string{}
	for ps, ms := range e.owners {
		if _, mine := ms[in.Manager]; !mine {
			continue
		}
		if _, had := before[ps]; !had {
			newOwners = append(newOwners, ps)
		}
	}
	sort.Strings(newOwners)

	prev := in.Live
	if prev == nil {
		prev = map[string]any{}
	}
	res := &Result{
		Live:      next,
		Owners:    e.owners,
		Applied:   cfg,
		Changed:   changed,
		Changes:   Diff(prev, next, &in.Schema),
		NewOwners: newOwners,
	}
	return res, nil
}

// stripNulls recursively removes explicit null members from objects (matching
// server-side-apply, where null encodes absence of opinion). Null elements of
// arrays are retained (set lists may legitimately contain them).
func stripNulls(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := map[string]any{}
		for k, cv := range t {
			if cv == nil {
				continue
			}
			out[k] = stripNulls(cv)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, cv := range t {
			if cv == nil {
				out[i] = nil
			} else {
				out[i] = stripNulls(cv)
			}
		}
		return out
	default:
		return v
	}
}

// reconcile walks live and desired values at path. cfg==nil means "path absent
// from the new config" — semantically different from an explicit value.
func (e *engine) reconcile(p model.Path, live, cfg any) any {
	_, liveArr := live.([]any)
	_, cfgArr := cfg.([]any)
	liveObj, _ := live.(map[string]any)
	cfgObj, _ := cfg.(map[string]any)

	switch {
	case isCompound(live) || isCompound(cfg):
		// Composite-kind collision (object vs array) is an atomic replacement.
		if live != nil && cfg != nil && (liveArr != cfgArr) {
			return e.replaceSubtree(p, live, cfg)
		}
		if cfgArr || liveArr {
			kind, _ := e.in.Schema.KindAt(p)
			switch kind {
			case model.ListSet:
				return e.reconcileSet(p, live, cfg)
			case model.ListKeyed:
				return e.reconcileKeyed(p, live, cfg)
			default:
				return e.reconcileAtomic(p, live, cfg)
			}
		}
		return e.reconcileObject(p, liveObj, cfgObj, false)
	default:
		return e.reconcileScalar(p, live, cfg)
	}
}

func (e *engine) reconcileObject(p model.Path, live, cfg map[string]any, root bool) any {
	names := map[string]struct{}{}
	for k := range live {
		names[k] = struct{}{}
	}
	for k := range cfg {
		names[k] = struct{}{}
	}
	out := map[string]any{}
	for _, name := range sortedNames(names) {
		cp := p.Child(model.FieldSeg(name))
		lv, inLive := live[name]
		cv, inCfg := cfg[name]
		switch {
		case inLive && inCfg:
			out[name] = e.reconcile(cp, lv, cv)
		case inCfg:
			out[name] = e.reconcile(cp, nil, cv)
		default:
			// Absent from desired: withdraw our shares. Anything still owned
			// by others, or never owned at all (uncommitted), survives.
			if kept := e.withdraw(cp, lv); kept != nil {
				out[name] = kept
			}
		}
	}
	if root || len(out) > 0 {
		return out
	}
	return nil
}

// reconcileKeyed merges arrays of objects identified by a scalar key field.
// Elements with equal keys merge field by field; elements the manager drops
// from its config are withdrawn, not forcibly deleted.
func (e *engine) reconcileKeyed(p model.Path, live, cfg any) any {
	_, keyField := e.in.Schema.KindAt(p)
	liveArr, _ := live.([]any)
	cfgArr, _ := cfg.([]any)

	type entry struct {
		obj map[string]any
		key any
		id  string
	}
	liveEntries := []entry{}
	liveById := map[string]int{}
	for i, el := range liveArr {
		obj, ok := el.(map[string]any)
		if !ok {
			continue // malformed live elements are preserved verbatim below
		}
		kv, ok := obj[keyField]
		if !ok || !isScalar(kv) {
			continue
		}
		liveById[scalarKey(kv)] = i
		liveEntries = append(liveEntries, entry{obj, kv, scalarKey(kv)})
	}
	cfgById := map[string]int{}
	for i, el := range cfgArr {
		obj := el.(map[string]any) // validated
		kv := obj[keyField]
		cfgById[scalarKey(kv)] = i
	}

	var out []any
	seen := map[string]struct{}{}
	for i, el := range liveArr {
		obj, ok := el.(map[string]any)
		if !ok {
			out = append(out, cloneValue(el)) // preserve unparseable live data
			continue
		}
		kv, hasKey := obj[keyField]
		if !hasKey || !isScalar(kv) {
			out = append(out, cloneValue(obj))
			continue
		}
		id := scalarKey(kv)
		seen[id] = struct{}{}
		ep := p.Child(model.KeySeg(keyField, string(model.MustRaw(kv))))
		if ci, present := cfgById[id]; present {
			merged := e.reconcile(ep, obj, cfgArr[ci])
			if merged == nil {
				e.owners.DropSubtree(ep.String())
				continue
			}
			mo := merged.(map[string]any)
			if _, stillKeyed := mo[keyField]; !stillKeyed {
				e.owners.DropSubtree(ep.String())
				continue
			}
			out = append(out, mo)
		} else {
			kept := e.withdraw(ep, obj)
			if kept != nil {
				out = append(out, kept)
			}
		}
		_ = i
	}
	for _, el := range cfgArr {
		obj := el.(map[string]any)
		kv := obj[keyField]
		id := scalarKey(kv)
		if _, dup := seen[id]; dup {
			continue
		}
		seen[id] = struct{}{}
		ep := p.Child(model.KeySeg(keyField, string(model.MustRaw(kv))))
		added := e.reconcile(ep, nil, obj)
		if added != nil {
			out = append(out, added)
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// reconcileSet merges arrays by scalar membership; each value is an owned leaf.
func (e *engine) reconcileSet(p model.Path, live, cfg any) any {
	liveArr, _ := live.([]any)
	cfgArr, _ := cfg.([]any)
	desired := map[string]any{}
	var desiredOrder []string
	for _, v := range cfgArr {
		id := scalarKey(v)
		if _, ok := desired[id]; !ok {
			desiredOrder = append(desiredOrder, id)
		}
		desired[id] = v
	}

	var out []any
	seen := map[string]struct{}{}
	for _, v := range liveArr {
		id := scalarKey(v)
		ep := p.Child(model.KeySeg(model.SetElementKey, id))
		if dv, ok := desired[id]; ok {
			seen[id] = struct{}{}
			e.setLeaf(ep, v, dv)
			out = append(out, cloneValue(dv))
		} else {
			if e.withdrawLeaf(ep) {
				continue // manager released the last share -> removed
			}
			out = append(out, cloneValue(v))
		}
	}
	for _, id := range desiredOrder {
		if _, ok := seen[id]; ok {
			continue
		}
		v := desired[id]
		ep := p.Child(model.KeySeg(model.SetElementKey, id))
		e.setLeaf(ep, nil, v)
		out = append(out, cloneValue(v))
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// reconcileAtomic handles whole-array atoms. The array is one owned leaf: any
// differing payload must be claimable wholesale.
func (e *engine) reconcileAtomic(p model.Path, live, cfg any) any {
	if cfg == nil {
		// Array absent from desired config: withdraw the atom.
		if e.withdrawLeaf(p) {
			return nil
		}
		return cloneValue(live)
	}
	if !EqualJSON(live, cfg) {
		others := e.owners.Others(p.String(), e.in.Manager)
		if len(others) > 0 && !e.in.Force {
			e.conflicts = append(e.conflicts, model.Conflict{
				Path:    p.String(),
				Owners:  others,
				Current: model.MustRaw(live),
				Applied: model.MustRaw(cfg),
			})
			return cloneValue(live)
		}
		if e.in.Force {
			for _, m := range others {
				e.owners.Remove(p.String(), m)
			}
		}
		e.changed = true
	}
	e.owners.Add(p.String(), e.in.Manager)
	return cloneValue(cfg)
}

func (e *engine) reconcileScalar(p model.Path, live, cfg any) any {
	if cfg == nil {
		// Scalar absent from desired config: withdraw the manager's share;
		// a co-owner's or uncommitted value survives (handled by caller).
		if e.withdrawLeaf(p) {
			return nil
		}
		return cloneValue(live)
	}
	e.setLeaf(p, live, cfg)
	return cloneValue(cfg)
}

// replaceSubtree handles an object<->array structural collision at p. Without
// force, every foreign-owned leaf of the live subtree blocks it. With force the
// live subtree's leaves are stripped first, then the desired value is written
// atomically at p.
func (e *engine) replaceSubtree(p model.Path, live, cfg any) any {
	if !e.canReplaceSubtree(p, live) {
		return cloneValue(live)
	}
	e.owners.DropSubtree(p.String())
	e.changed = true
	if isCompound(cfg) {
		if _, isArr := cfg.([]any); isArr {
			kind, _ := e.in.Schema.KindAt(p)
			if kind == model.ListSet {
				return e.reconcileSet(p, nil, cfg)
			}
			if kind == model.ListKeyed {
				return e.reconcileKeyed(p, nil, cfg)
			}
		}
	}
	// Claim every leaf of the replacement subtree.
	claimLeaves(cfg, p, &e.in.Schema, e.owners, e.in.Manager)
	return cloneValue(cfg)
}

func (e *engine) canReplaceSubtree(p model.Path, live any) bool {
	leaves := map[string]any{}
	collectLeaves(live, p, &e.in.Schema, leaves)
	ok := true
	for ps, lv := range leaves {
		others := e.owners.Others(ps, e.in.Manager)
		if len(others) == 0 {
			continue
		}
		if e.in.Force {
			for _, m := range others {
				e.owners.Remove(ps, m)
			}
			e.ownersMoved = true
			continue
		}
		e.conflicts = append(e.conflicts, model.Conflict{
			Path:    ps,
			Owners:  others,
			Current: model.MustRaw(lv),
			Applied: model.MustRaw(nil),
		})
		ok = false
	}
	return ok
}

// setLeaf applies the per-leaf ownership/value rule:
//
//	desired == live                    -> share ownership (no conflict)
//	owned by other managers, !force    -> conflict, live untouched
//	owned by other managers, force     -> strip others, claim, write
//	manager owns it / nobody owns it   -> claim, write if different
func (e *engine) setLeaf(p model.Path, live, cfg any) {
	ps := p.String()
	if _, alreadyOwner := e.before[ps]; !alreadyOwner {
		e.ownersMoved = true
	}
	if !EqualJSON(live, cfg) {
		others := e.owners.Others(ps, e.in.Manager)
		if len(others) > 0 {
			if !e.in.Force {
				e.conflicts = append(e.conflicts, model.Conflict{
					Path:    ps,
					Owners:  others,
					Current: model.MustRaw(live),
					Applied: model.MustRaw(cfg),
				})
				return
			}
			for _, m := range others {
				e.owners.Remove(ps, m)
			}
		}
		e.changed = true
	}
	e.owners.Add(ps, e.in.Manager)
}

// withdraw releases the manager's ownership at an omitted path. nil means
// "prune from parent"; otherwise the residual kept by others is returned.
func (e *engine) withdraw(cp model.Path, live any) any {
	if live == nil {
		return nil
	}
	if !e.ownsAnything(cp.String()) {
		return cloneValue(live) // uncommitted or other-managed: untouched
	}
	if isCompound(live) {
		return e.withdrawCompound(cp, live)
	}
	if e.withdrawLeaf(cp) {
		return nil
	}
	return cloneValue(live)
}

func (e *engine) ownsAnything(prefix string) bool {
	for k, ms := range e.owners {
		if !model.HasPathPrefix(k, prefix) {
			continue
		}
		if _, ok := ms[e.in.Manager]; ok {
			return true
		}
	}
	return false
}

func (e *engine) withdrawCompound(p model.Path, live any) any {
	if obj, ok := live.(map[string]any); ok {
		out := map[string]any{}
		for _, name := range sortedMap(obj) {
			if kept := e.withdraw(p.Child(model.FieldSeg(name)), obj[name]); kept != nil {
				out[name] = kept
			}
		}
		if len(out) == 0 {
			return nil
		}
		return out
	}
	arr := live.([]any)
	kind, keyField := e.in.Schema.KindAt(p)
	var out []any
	switch kind {
	case model.ListSet:
		for _, v := range arr {
			ep := p.Child(model.KeySeg(model.SetElementKey, scalarKey(v)))
			if e.withdrawLeaf(ep) {
				continue
			}
			out = append(out, cloneValue(v))
		}
	case model.ListKeyed:
		for _, el := range arr {
			obj, ok := el.(map[string]any)
			if !ok {
				out = append(out, cloneValue(el))
				continue
			}
			kv, hasKey := obj[keyField]
			if !hasKey || !isScalar(kv) {
				out = append(out, cloneValue(el))
				continue
			}
			ep := p.Child(model.KeySeg(keyField, string(model.MustRaw(kv))))
			if kept := e.withdraw(ep, obj); kept != nil {
				out = append(out, kept)
			}
		}
	default:
		if e.withdrawLeaf(p) {
			return nil
		}
		return cloneValue(live)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// withdrawLeaf releases the manager's share of a leaf; true when no owners
// remain (the field must be pruned).
func (e *engine) withdrawLeaf(p model.Path) bool {
	ps := p.String()
	set := e.owners[ps]
	if set == nil {
		return false
	}
	if _, mine := set[e.in.Manager]; !mine {
		return false
	}
	empty := e.owners.Remove(ps, e.in.Manager)
	e.changed = true
	return empty
}

// -----------------------------------------------------------------------------
// Diff (auditable per-leaf change set)
// -----------------------------------------------------------------------------

// Diff computes schema-aware leaf-level differences between two live states.
func Diff(before, after any, schema *model.Schema) model.ChangeSet {
	a := map[string]any{}
	b := map[string]any{}
	collectLeaves(before, model.Path{}, schema, a)
	collectLeaves(after, model.Path{}, schema, b)
	var cs model.ChangeSet
	for ps, bv := range b {
		av, existed := a[ps]
		if !existed {
			cs.Added = append(cs.Added, model.Change{Path: ps, New: model.MustRaw(bv)})
			continue
		}
		if !EqualJSON(av, bv) {
			cs.Changed = append(cs.Changed, model.Change{
				Path: ps, Old: model.MustRaw(av), New: model.MustRaw(bv),
			})
		}
	}
	for ps, av := range a {
		if _, exists := b[ps]; !exists {
			cs.Removed = append(cs.Removed, model.Change{Path: ps, Old: model.MustRaw(av)})
		}
	}
	sortChanges := func(c []model.Change) {
		sort.SliceStable(c, func(i, j int) bool { return c[i].Path < c[j].Path })
	}
	sortChanges(cs.Added)
	sortChanges(cs.Changed)
	sortChanges(cs.Removed)
	return cs
}

// Leaves returns the schema-aware leaves below v as path -> value.
func Leaves(v any, schema *model.Schema) map[string]any {
	out := map[string]any{}
	collectLeaves(v, model.Path{}, schema, out)
	return out
}

// collectLeaves enumerates schema-aware leaves below v rooted at p.
func collectLeaves(v any, p model.Path, schema *model.Schema, out map[string]any) {
	switch t := v.(type) {
	case nil:
		if len(p) > 0 { // the empty root {} carries no leaves
			out[p.String()] = nil
		}
	case map[string]any:
		if len(t) == 0 {
			if len(p) > 0 {
				out[p.String()] = t
			}
			return
		}
		for _, k := range sortedMap(t) {
			collectLeaves(t[k], p.Child(model.FieldSeg(k)), schema, out)
		}
	case []any:
		kind, keyField := schema.KindAt(p)
		switch kind {
		case model.ListAtomic:
			out[p.String()] = t
		case model.ListSet:
			for _, el := range t {
				out[p.Child(model.KeySeg(model.SetElementKey, scalarKey(el))).String()] = el
			}
		case model.ListKeyed:
			for _, el := range t {
				obj, ok := el.(map[string]any)
				if !ok {
					out[p.String()] = t // malformed: treat whole array atomically
					return
				}
				kv, ok := obj[keyField]
				if !ok || !isScalar(kv) {
					out[p.String()] = t
					return
				}
				collectLeaves(el, p.Child(model.KeySeg(keyField, string(model.MustRaw(kv)))), schema, out)
			}
		}
	default:
		out[p.String()] = t
	}
}

// claimLeaves records the manager as owner of every leaf in a replacement.
func claimLeaves(v any, p model.Path, schema *model.Schema, owners model.Owners, manager string) {
	leaves := map[string]any{}
	collectLeaves(v, p, schema, leaves)
	for ps := range leaves {
		owners.Add(ps, manager)
	}
}

// -----------------------------------------------------------------------------
// Validation
// -----------------------------------------------------------------------------

func validateValue(v any, p model.Path, schema *model.Schema) error {
	switch t := v.(type) {
	case map[string]any:
		for _, k := range sortedMap(t) {
			if err := validateValue(t[k], p.Child(model.FieldSeg(k)), schema); err != nil {
				return err
			}
		}
	case []any:
		kind, keyField := schema.KindAt(p)
		switch kind {
		case model.ListSet:
			for i, el := range t {
				if !isScalar(el) {
					return fail(model.CatInvalidInput, "set_element_not_scalar",
						fmt.Sprintf("%s[%d]: set lists may contain only scalar values", p, i))
				}
			}
		case model.ListKeyed:
			seen := map[string]struct{}{}
			for i, el := range t {
				obj, ok := el.(map[string]any)
				if !ok {
					return fail(model.CatInvalidInput, "keyed_element_not_object",
						fmt.Sprintf("%s[%d]: keyed lists may contain only objects", p, i))
				}
				kv, ok := obj[keyField]
				if !ok {
					return fail(model.CatInvalidInput, "key_missing",
						fmt.Sprintf("%s[%d]: key field %q is required", p, i, keyField))
				}
				if !isScalar(kv) {
					return fail(model.CatInvalidInput, "key_not_scalar",
						fmt.Sprintf("%s[%d]: key field %q must be scalar", p, i, keyField))
				}
				id := scalarKey(kv)
				if _, dup := seen[id]; dup {
					return fail(model.CatInvalidInput, "duplicate_key",
						fmt.Sprintf("%s[%d]: duplicate key %s=%s", p, i, keyField, id))
				}
				seen[id] = struct{}{}
				if err := validateValue(el, p.Child(model.KeySeg(keyField, id)), schema); err != nil {
					return err
				}
			}
		default:
			for i, el := range t {
				if err := validateValue(el, p.Child(model.FieldSeg(fmt.Sprintf("[%d]", i))), schema); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// -----------------------------------------------------------------------------
// Shared helpers
// -----------------------------------------------------------------------------

func fail(c model.Category, code, msg string) *model.Error {
	return &model.Error{Category: c, Code: code, Message: msg}
}

func isCompound(v any) bool {
	switch v.(type) {
	case map[string]any, []any:
		return true
	}
	return false
}

func isScalar(v any) bool {
	switch v.(type) {
	case nil, string, bool, json.Number:
		return true
	}
	return false
}

func scalarKey(v any) string { return string(model.MustRaw(v)) }

func sortedNames(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func sortedMap(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func cloneValue(v any) any {
	if v == nil {
		return nil
	}
	raw, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Sprintf("merge: clone marshal: %v", err))
	}
	out, err := model.DecodeValue(raw)
	if err != nil {
		panic(fmt.Sprintf("merge: clone decode: %v", err))
	}
	return out
}

// EqualJSON compares decoded JSON values semantically: json.Numbers compare by
// numeric value (1 == 1.0), everything else by type and structure.
func EqualJSON(a, b any) bool {
	if an, ok := a.(json.Number); ok {
		bn, ok := b.(json.Number)
		if !ok {
			return false
		}
		return numbersEqual(an, bn)
	}
	switch av := a.(type) {
	case map[string]any:
		bv, ok := b.(map[string]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for k, v := range av {
			if !EqualJSON(v, bv[k]) {
				return false
			}
		}
		return true
	case []any:
		bv, ok := b.([]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for i := range av {
			if !EqualJSON(av[i], bv[i]) {
				return false
			}
		}
		return true
	case nil:
		return b == nil
	case bool:
		bv, ok := b.(bool)
		return ok && av == bv
	case string:
		bv, ok := b.(string)
		return ok && av == bv
	default:
		return a == b
	}
}

func numbersEqual(a, b json.Number) bool {
	ar, ok1 := new(big.Rat).SetString(string(a))
	br, ok2 := new(big.Rat).SetString(string(b))
	if ok1 && ok2 {
		return ar.Cmp(br) == 0
	}
	return a == b
}
