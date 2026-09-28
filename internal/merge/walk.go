package merge

import (
	"sort"

	"fieldmerge/internal/apperr"
	"fieldmerge/internal/fieldpath"
)

// walkObject merges one JSON object level.
//
//	cfg           manager's declared object; nil means the manager did not
//	              submit this object (fields inside are retracted one by one
//	              if the manager owns them, otherwise left untouched)
//	live          current merged object (nil if absent)
//	path          ownership path of this object
//	schemaFields  dotted field chain for schema lookup (list key segments are
//	              NOT part of it)
//	idField       when non-empty, names the identity key field of a keyed-list
//	              element at THIS level: it is part of the field path, not a
//	              claimed value, so it is never owned/changed/contested
//
// Returns the merged value, whether the field is still alive (false => prune
// it from the parent) and an aborting error (invalid input / corrupt state).
func (e *engine) walkObject(cfg map[string]any, live map[string]any,
	path fieldpath.Path, schemaFields []string, idField string) (any, bool, error) {

	result := map[string]any{}
	if live != nil {
		for k, v := range live {
			result[k] = v
		}
	}

	names := map[string]struct{}{}
	for k := range live {
		names[k] = struct{}{}
	}
	for k := range cfg {
		names[k] = struct{}{}
	}
	ordered := make([]string, 0, len(names))
	for k := range names {
		ordered = append(ordered, k)
	}
	sort.Strings(ordered)

	for _, name := range ordered {
		fp := path.PushField(name)
		sp := append(append([]string{}, schemaFields...), name)

		liveVal, liveOK := mapLookup(live, name)
		cv := absentCfg
		if cfg != nil {
			if v, ok := cfg[name]; ok {
				cv = presentCfg(v)
			}
		}

		// The keyed-list identity key lives in the path, not the claim set.
		// It is always retained verbatim and exempt from ownership rules.
		if idField != "" && name == idField {
			v := liveVal
			if cv.present && v == nil {
				v = cv.value
			}
			result[name] = v
			continue
		}

		decl, hasDecl := e.schema.Lookup(sp)

		var (
			out   any
			alive bool
			err   error
		)
		switch {
		case hasDecl && decl.Type == "set":
			out, alive, err = e.walkSet(cv, asSlice(liveVal), liveOK, fp)
		case hasDecl && decl.Type == "map":
			out, alive, err = e.walkMapList(cv, asSlice(liveVal), liveOK, fp, sp, decl.KeyName)
		case hasDecl && decl.Type == "atomic":
			out, alive, err = e.handleAtomic(cv, liveVal, liveOK, fp)
		default:
			out, alive, err = e.walkUndeclared(cv, liveVal, liveOK, fp, sp)
		}
		if err != nil {
			return nil, false, err
		}
		if alive {
			result[name] = out
		} else {
			delete(result, name)
		}
	}

	return result, len(result) > 0, nil
}

// walkUndeclared handles fields without a list declaration: plain objects are
// deep-merged, every other value is an atomic leaf, and a list in config is a
// hard client error (clients must declare list semantics first).
func (e *engine) walkUndeclared(cv cfgVal, liveVal any, liveOK bool,
	path fieldpath.Path, schemaFields []string) (any, bool, error) {

	if cv.present {
		if _, isList := cv.value.([]any); isList {
			return nil, false, apperr.New(apperr.InvalidInput, "undeclared_list",
				"field %s is a list but the %q schema declares no list semantics for it",
				path, e.in.Kind)
		}
		if cfgObj, isObj := cv.value.(map[string]any); isObj {
			// object config over a non-object live = structural replacement;
			// a foreign claim at/under this node blocks it unless force.
			if liveOK {
				if _, liveObj := liveVal.(map[string]any); !liveObj {
					foreign := e.foreignUnder(path)
					if len(foreign) > 0 {
						if !e.force {
							e.addConflict(path, foreign, ReasonReplaceSubtree, "replace")
							return liveVal, true, nil
						}
						e.dropForeignUnder(path)
						e.addChange(path, "takeover", "force_replace_subtree", liveVal, cv.value)
					}
				}
			}
			out, alive, err := e.walkObject(cfgObj, asMap(liveVal), path, schemaFields, "")
			if err != nil {
				return nil, false, err
			}
			// New object subtree: claim every leaf introduced below.
			if liveOK {
				if _, liveObj := liveVal.(map[string]any); !liveObj {
					e.claimNewSubtree(out, path)
				}
			}
			return out, alive, nil
		}
		// scalar (or explicit null) leaf
		return e.handleAtomic(cv, liveVal, liveOK, path)
	}

	// Absent: recurse into live objects so owned leaves deeper down can be
	// retracted; other shapes are atomic leaves from the ownership view.
	if liveObj, isObj := liveVal.(map[string]any); isObj {
		return e.walkObject(nil, liveObj, path, schemaFields, "")
	}
	return e.handleAtomic(cv, liveVal, liveOK, path)
}

// claimNewSubtree records ownership of every leaf reachable under v for the
// applying manager after a structural replacement the manager won.
func (e *engine) claimNewSubtree(v any, base fieldpath.Path) {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			e.claimNewSubtree(t[k], base.PushField(k))
		}
	case []any:
		e.setOwners(base, e.mgr) // lists under replacement are taken opaquely
	default:
		e.setOwners(base, e.mgr)
	}
}

// handleAtomic covers scalar leaves, undeclared lists and arbitrary subtrees
// replaced wholesale. Distinguishes three intents:
//
//   - present non-null: set
//   - present null:     explicit delete (different from omission)
//   - absent:           retract — only affects fields this manager owns
func (e *engine) handleAtomic(cv cfgVal, liveVal any, liveOK bool,
	path fieldpath.Path) (any, bool, error) {

	switch {
	case cv.present && cv.value != nil:
		// SET (possibly replacing a subtree of a different shape).
		foreign := e.foreignUnder(path)
		sameShape := shapeEqual(cv.value, liveVal)
		if len(foreign) > 0 && !deepEqual(cv.value, liveVal) {
			reason := ReasonAtomicMismatch
			if !sameShape {
				reason = ReasonReplaceSubtree
			}
			if !e.force {
				e.addConflict(path, foreign, reason, "set")
				return liveVal, liveOK, nil
			}
			e.dropForeignUnder(path)
			if liveOK {
				e.addChange(path, "takeover", "force", liveVal, cv.value)
			} else {
				e.addChange(path, "set", "force_new", nil, cv.value)
			}
			e.setOwners(path, e.mgr)
			return cv.value, true, nil
		}
		e.setOwners(path, mergeOwners(e.managersAt(path), e.mgr)...)
		if !liveOK || !deepEqual(cv.value, liveVal) {
			e.addChange(path, "set", "", liveVal, cv.value)
		} else if len(foreign) > 0 {
			e.addChange(path, "share", "equal_value_owned_by_other", liveVal, cv.value)
		}
		return cv.value, true, nil

	case cv.present: // value == nil => explicit JSON null
		if !liveOK {
			return nil, false, nil
		}
		foreign := e.foreignUnder(path)
		if len(foreign) > 0 {
			if !e.force {
				e.addConflict(path, foreign, ReasonExplicitDelete, "delete")
				return liveVal, true, nil
			}
			e.dropForeignUnder(path)
			e.setOwners(path)
			e.addChange(path, "takeover", "force_delete", liveVal, nil)
			return nil, false, nil
		}
		reason := "explicit"
		if !ownsUnder(e, path) {
			reason = "explicit_unowned"
		}
		e.releaseUnder(path)
		e.addChange(path, "delete", reason, liveVal, nil)
		return nil, false, nil

	default:
		// ABSENT — retract only what this manager (co-)owns.
		if !liveOK {
			return nil, false, nil
		}
		foreign := e.foreignUnder(path)
		selfOwns := ownsUnder(e, path)
		switch {
		case len(foreign) > 0 && selfOwns:
			if !e.force {
				e.addConflict(path, foreign, ReasonRetract, "retract")
				return liveVal, true, nil
			}
			e.dropForeignUnder(path)
			e.releaseUnder(path)
			e.addChange(path, "takeover", "force_retract", liveVal, nil)
			return nil, false, nil
		case len(foreign) > 0:
			// Owned by others, not by us: omission is not a deletion.
			return liveVal, true, nil
		case selfOwns:
			e.releaseUnder(path)
			e.addChange(path, "delete", "retract", liveVal, nil)
			return nil, false, nil
		default:
			// Unowned live value: untouched.
			return liveVal, true, nil
		}
	}
}

// releaseUnder drops this manager from the exact claim and every descendant
// claim of path.
func (e *engine) releaseUnder(path fieldpath.Path) {
	for cp, set := range e.claims {
		cfp, _ := fieldpath.Parse(cp)
		if cfp.HasPrefix(path) {
			delete(set, e.mgr)
			if len(set) == 0 {
				delete(e.claims, cp)
			}
		}
	}
}

// ownsUnder reports whether the applying manager holds the exact claim or any
// descendant claim of path.
func ownsUnder(e *engine, path fieldpath.Path) bool {
	for cp, set := range e.claims {
		cfp, _ := fieldpath.Parse(cp)
		if cfp.HasPrefix(path) {
			if _, ok := set[e.mgr]; ok {
				return true
			}
		}
	}
	return false
}

// managersAt returns the sorted owner set of an exact claim path.
func (e *engine) managersAt(path fieldpath.Path) []string {
	var out []string
	for m := range e.claims[path.String()] {
		out = append(out, m)
	}
	sort.Strings(out)
	return out
}

func mapLookup(m map[string]any, k string) (any, bool) {
	if m == nil {
		return nil, false
	}
	v, ok := m[k]
	return v, ok
}

// mergeOwners returns existing plus self, sorted and deduplicated.
func mergeOwners(existing []string, self string) []string {
	seen := map[string]struct{}{}
	for _, m := range existing {
		seen[m] = struct{}{}
	}
	seen[self] = struct{}{}
	out := make([]string, 0, len(seen))
	for m := range seen {
		out = append(out, m)
	}
	sort.Strings(out)
	return out
}

// shapeEqual reports whether a and b are the same JSON kind (object vs array
// vs scalar). Scalars of different types count as the same shape — a value
// mismatch is reported with ReasonAtomicMismatch instead.
func shapeEqual(a, b any) bool {
	if a == nil || b == nil {
		return a == b
	}
	switch a.(type) {
	case map[string]any:
		_, ok := b.(map[string]any)
		return ok
	case []any:
		_, ok := b.([]any)
		return ok
	default:
		switch b.(type) {
		case map[string]any, []any:
			return false
		default:
			return true
		}
	}
}
