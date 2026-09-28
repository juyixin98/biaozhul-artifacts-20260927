package merge

import (
	"sort"

	"fieldmerge/internal/apperr"
	"fieldmerge/internal/fieldpath"
)

// Reason code for removing a set value owned by another manager.
const ReasonRemoveSetElement = "removes_set_value_owned_by_other"

// walkSet merges a list with set semantics: unordered, deduplicated scalar
// values; ownership is per value and several managers may share one value.
func (e *engine) walkSet(cv cfgVal, live []any, liveOK bool,
	path fieldpath.Path) (any, bool, error) {

	if cv.present && cv.value == nil {
		// Explicit null deletes the whole field.
		return e.handleAtomic(cv, anySlice(live), liveOK, path)
	}

	if !cv.present {
		// Retract pass: keep live order, removing only values this manager
		// (co-)owns. A shared value removal conflicts; a foreign-only value
		// is untouched.
		out := []any{}
		for _, v := range live {
			tok, err := fieldpath.Token(v)
			if err != nil {
				return nil, false, apperr.New(apperr.Internal, "set_corrupt",
					"live set at %s holds a non-scalar value", path)
			}
			vp := path.PushSet(tok)
			if !selfOwnsExact(e, vp) {
				out = append(out, v)
				continue
			}
			foreign := e.foreignExact(vp)
			if len(foreign) > 0 {
				if !e.force {
					e.addConflict(vp, foreign, ReasonRemoveSetElement, "retract")
					out = append(out, v)
					continue
				}
				e.dropForeignUnder(vp)
			}
			e.releaseUnder(vp)
			e.addChange(vp, "delete", "retract_set_value", v, nil)
		}
		return out, len(out) > 0, nil
	}

	decl, ok := cv.value.([]any)
	if !ok {
		return nil, false, apperr.New(apperr.InvalidInput, "set_not_array",
			"field %s is declared as a set but config value is %T", path, cv.value)
	}
	cfgTokens := map[string]any{}
	cfgOrder := []string{}
	for _, v := range decl {
		tok, err := fieldpath.Token(v)
		if err != nil {
			return nil, false, apperr.New(apperr.InvalidInput, "set_value_not_scalar",
				"set at %s contains non-scalar element", path)
		}
		if _, dup := cfgTokens[tok]; dup {
			return nil, false, apperr.New(apperr.InvalidInput, "set_duplicate_value",
				"set at %s contains duplicate value %s", path, tok)
		}
		cfgTokens[tok] = v
		cfgOrder = append(cfgOrder, tok)
	}

	liveTokens := map[string]any{}
	for _, v := range live {
		tok, err := fieldpath.Token(v)
		if err != nil {
			return nil, false, apperr.New(apperr.Internal, "set_corrupt",
				"live set at %s holds a non-scalar value", path)
		}
		liveTokens[tok] = v
	}

	// Removals: live values omitted by the declaration. A value is removed
	// only when the applying manager owns it (omission retracts what is
	// yours); foreign-only and unowned values survive — omission is not an
	// explicit delete.
	survivors := []any{}
	for _, v := range live {
		tok, _ := fieldpath.Token(v)
		if _, wanted := cfgTokens[tok]; wanted {
			continue
		}
		vp := path.PushSet(tok)
		switch {
		case selfOwnsExact(e, vp) && len(e.foreignExact(vp)) > 0:
			if !e.force {
				e.addConflict(vp, e.foreignExact(vp), ReasonRemoveSetElement, "delete")
				survivors = append(survivors, v) // shared value survives the conflict
				continue
			}
			e.dropForeignUnder(vp)
			e.releaseUnder(vp)
			e.addChange(vp, "takeover", "force_remove_set_value", v, nil)
		case selfOwnsExact(e, vp):
			e.releaseUnder(vp)
			e.addChange(vp, "delete", "set_value_retracted", v, nil)
		default:
			// Owned by others or unowned: omission is not a deletion.
			survivors = append(survivors, v)
		}
	}

	// Additions / shares in declaration order, then surviving foreign values.
	out := make([]any, 0, len(cfgOrder)+len(survivors))
	emitted := map[string]struct{}{}
	for _, tok := range cfgOrder {
		v := cfgTokens[tok]
		vp := path.PushSet(tok)
		if _, exists := liveTokens[tok]; exists {
			e.setOwners(vp, mergeOwners(e.managersAt(vp), e.mgr)...)
			if len(e.foreignExact(vp)) > 0 {
				e.addChange(vp, "share", "equal_value_owned_by_other", v, v)
			}
		} else {
			e.setOwners(vp, e.mgr)
			e.addChange(vp, "set", "new_set_value", nil, v)
		}
		out = append(out, v)
		emitted[tok] = struct{}{}
	}
	for _, v := range survivors {
		tok, _ := fieldpath.Token(v)
		if _, done := emitted[tok]; done {
			continue
		}
		out = append(out, v)
		emitted[tok] = struct{}{}
	}
	return out, len(out) > 0, nil
}

// walkMapList merges a keyed list ("map"): elements are objects identified by
// keyName and merge independently, element by element. schemaFields is the
// dotted field chain up to and including the list field.
func (e *engine) walkMapList(cv cfgVal, live []any, liveOK bool,
	path fieldpath.Path, schemaFields []string, keyName string) (any, bool, error) {

	if cv.present && cv.value == nil {
		return e.handleAtomic(cv, anySlice(live), liveOK, path)
	}

	indexLive := func(els []any) (map[string]map[string]any, error) {
		byToken := map[string]map[string]any{}
		for _, el := range els {
			m, ok := el.(map[string]any)
			if !ok {
				return nil, apperr.New(apperr.Internal, "map_list_corrupt",
					"live map-list at %s holds a non-object element", path)
			}
			kv, ok := m[keyName]
			if !ok || kv == nil {
				return nil, apperr.New(apperr.Internal, "map_list_corrupt",
					"live map-list at %s has an element without key %q", path, keyName)
			}
			tok, err := fieldpath.Token(kv)
			if err != nil {
				return nil, apperr.New(apperr.Internal, "map_list_corrupt",
					"live map-list at %s has non-scalar key %q: %v", path, keyName, err)
			}
			if _, dup := byToken[tok]; dup {
				return nil, apperr.New(apperr.Internal, "map_list_duplicate_live_key",
					"live map-list at %s contains two elements with %s=%s",
					path, keyName, tok)
			}
			byToken[tok] = m
		}
		return byToken, nil
	}

	if !cv.present {
		// Retract pass: walk each live element with an absent config object,
		// which performs field-level retraction and preserves foreign fields
		// and the key itself.
		if _, err := indexLive(live); err != nil {
			return nil, false, err
		}
		out := []any{}
		for _, elAny := range live {
			el := elAny.(map[string]any)
			tok, _ := fieldpath.Token(el[keyName])
			ep := path.PushKey(keyName, tok)
			merged, alive, err := e.walkObject(nil, el, ep, schemaFields, keyName)
			if err != nil {
				return nil, false, err
			}
			if m, keep := e.elementAfterMerge(ep, el, merged, alive, keyName); keep {
				out = append(out, m)
			}
		}
		return out, len(out) > 0, nil
	}

	decl, ok := cv.value.([]any)
	if !ok {
		return nil, false, apperr.New(apperr.InvalidInput, "map_list_not_array",
			"field %s is declared as a keyed list but config value is %T", path, cv.value)
	}
	cfgByToken := map[string]map[string]any{}
	cfgOrder := []string{}
	for _, el := range decl {
		if el == nil {
			return nil, false, apperr.New(apperr.InvalidInput, "map_list_null_element",
				"keyed list at %s contains a null element; omit it to remove", path)
		}
		m, ok := el.(map[string]any)
		if !ok {
			return nil, false, apperr.New(apperr.InvalidInput, "map_list_element_not_object",
				"keyed list at %s contains a non-object element", path)
		}
		kv, ok := m[keyName]
		if !ok || kv == nil {
			return nil, false, apperr.New(apperr.InvalidInput, "map_list_missing_key",
				"keyed list at %s: every element must carry key %q", path, keyName)
		}
		tok, err := fieldpath.Token(kv)
		if err != nil {
			return nil, false, apperr.New(apperr.InvalidInput, "map_list_bad_key",
				"keyed list at %s: key %q must be scalar: %v", path, keyName, err)
		}
		if _, dup := cfgByToken[tok]; dup {
			return nil, false, apperr.New(apperr.InvalidInput, "map_list_duplicate_key",
				"keyed list at %s contains duplicate key %s=%s", path, keyName, tok)
		}
		cfgByToken[tok] = m
		cfgOrder = append(cfgOrder, tok)
	}

	liveByToken, err := indexLive(live)
	if err != nil {
		return nil, false, err
	}

	out := []any{}

	// Existing elements in live order.
	for _, elAny := range live {
		el := elAny.(map[string]any)
		tok, _ := fieldpath.Token(el[keyName])
		ep := path.PushKey(keyName, tok)
		cfgEl, declared := cfgByToken[tok]
		if !declared {
			// Omitted element: field-level retraction over its fields.
			merged, alive, err := e.walkObject(nil, el, ep, schemaFields, keyName)
			if err != nil {
				return nil, false, err
			}
			if m, keep := e.elementAfterMerge(ep, el, merged, alive, keyName); keep {
				out = append(out, m)
			}
			continue
		}
		merged, alive, err := e.walkObject(cfgEl, el, ep, schemaFields, keyName)
		if err != nil {
			return nil, false, err
		}
		if m, keep := e.elementAfterMerge(ep, el, merged, alive, keyName); keep {
			out = append(out, m)
		}
	}

	// New elements in declaration order.
	for _, tok := range cfgOrder {
		if _, exists := liveByToken[tok]; exists {
			continue
		}
		ep := path.PushKey(keyName, tok)
		merged, alive, err := e.walkObject(cfgByToken[tok], nil, ep, schemaFields, keyName)
		if err != nil {
			return nil, false, err
		}
		if m, keep := e.elementAfterMerge(ep, nil, merged, alive, keyName); keep {
			out = append(out, m)
		}
	}
	return out, len(out) > 0, nil
}

func (e *engine) foreignExact(p fieldpath.Path) []string {
	var out []string
	for m := range e.claims[p.String()] {
		if m != e.mgr {
			out = append(out, m)
		}
	}
	sort.Strings(out)
	return out
}

func selfOwnsExact(e *engine, p fieldpath.Path) bool {
	_, ok := e.claims[p.String()][e.mgr]
	return ok
}

// elementAfterMerge post-processes one merged keyed-list element: the
// identity key is always retained while the element lives; an element that
// shrinks to ONLY its key carries no real value anymore and is pruned (its
// claims are released) instead of emitting a {"key": ...} stub.
func (e *engine) elementAfterMerge(ep fieldpath.Path, liveEl map[string]any,
	merged any, alive bool, keyName string) (map[string]any, bool) {

	if !alive {
		return nil, false
	}
	m, ok := merged.(map[string]any)
	if !ok {
		return nil, false
	}
	if _, hasKey := m[keyName]; !hasKey && liveEl != nil {
		m[keyName] = liveEl[keyName]
	}
	if len(m) <= 1 {
		if ownsUnder(e, ep) {
			e.releaseUnder(ep)
			e.addChange(ep, "delete", "element_retracted_to_identity_only", liveEl, nil)
		}
		return nil, false
	}
	return m, true
}

func anySlice(v []any) any {
	if v == nil {
		return nil
	}
	return v
}
