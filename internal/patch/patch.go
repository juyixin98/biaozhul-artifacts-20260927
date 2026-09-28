// Package patch implements the small RFC 6902-style JSON patch subset that
// mutators use to modify documents, together with the declared-path guard.
//
// A mutator does not touch the document map directly: it returns PatchOps, the
// framework applies them, and every target path must match one of the paths
// the plugin declared at registration. A write outside those paths fails the
// whole admission as ReasonIllegalPath — partial application never commits.
package patch

import (
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"admission/internal/model"
)

// ErrIllegalPath marks a write to a path the plugin did not declare (or to a
// protected envelope path). The pipeline translates it to
// model.ReasonIllegalPath, which is always fatal regardless of fail policy.
var ErrIllegalPath = errors.New("illegal patch path")

// Protected paths can never be modified by any mutator: identity and apiVersion
// are fixed by the request.
var protected = []string{
	"/apiVersion",
	"/kind",
	"/metadata/name",
	"/metadata/namespace",
}

// Parse splits a JSON pointer into decoded tokens. The leading slash is
// required; "" is treated as the document root (not writable here).
func Parse(ptr string) ([]string, error) {
	if ptr == "" {
		return nil, errors.New("empty pointer targets the whole document")
	}
	if ptr[0] != '/' {
		return nil, fmt.Errorf("pointer %q must start with /", ptr)
	}
	raw := strings.Split(ptr[1:], "/")
	tokens := make([]string, len(raw))
	for i, t := range raw {
		t = strings.ReplaceAll(t, "~1", "/")
		t = strings.ReplaceAll(t, "~0", "~")
		tokens[i] = t
	}
	return tokens, nil
}

// Allowed checks that ptr is writable for a plugin that declared the given
// paths. Declared entries are either exact pointers ("/spec/replicas") or
// prefixes ending in "/*" which authorize any value underneath
// ("/metadata/annotations/*").
func Allowed(ptr string, declared []string) error {
	for _, p := range protected {
		if ptr == p {
			return fmt.Errorf("%w: %s is protected", ErrIllegalPath, ptr)
		}
	}
	tokens, err := Parse(ptr)
	if err != nil {
		return err
	}
	for _, d := range declared {
		if matchDeclared(tokens, d) {
			return nil
		}
	}
	return fmt.Errorf("%w: %s is not among declared paths %v", ErrIllegalPath, ptr, declared)
}

func matchDeclared(tokens []string, declared string) bool {
	if strings.HasSuffix(declared, "/*") {
		prefix := strings.TrimSuffix(declared, "/*")
		pt, err := Parse(prefix)
		if err != nil {
			return false
		}
		// A subtree declaration authorizes only strict descendants: the
		// collection object itself ("/metadata/annotations") is not writable.
		if len(tokens) <= len(pt) {
			return false
		}
		for i := range pt {
			if tokens[i] != pt[i] {
				return false
			}
		}
		return true
	}
	dt, err := Parse(declared)
	if err != nil {
		return false
	}
	if len(tokens) != len(dt) {
		return false
	}
	for i := range dt {
		if tokens[i] != dt[i] {
			return false
		}
	}
	return true
}

// Apply mutates doc in place according to ops after validating each path
// against declared. It applies ops sequentially; on the first error the
// document may be partially modified, so the caller must work on a deep copy
// and discard it on error (the pipeline does exactly this — nothing partial is
// ever committed).
func Apply(doc map[string]any, ops []model.PatchOp, declared []string) (bool, error) {
	changed := false
	for _, op := range ops {
		if err := Allowed(op.Path, declared); err != nil {
			return false, err
		}
		c, err := applyOne(doc, op)
		if err != nil {
			return false, fmt.Errorf("patch %s %s: %w", op.Op, op.Path, err)
		}
		changed = changed || c
	}
	return changed, nil
}

func applyOne(doc map[string]any, op model.PatchOp) (bool, error) {
	tokens, err := Parse(op.Path)
	if err != nil {
		return false, err
	}
	parent, key, err := navigate(doc, tokens)
	if err != nil {
		return false, err
	}
	switch op.Op {
	case "add", "replace":
		if op.Op == "replace" {
			if _, ok := parent[key]; !ok {
				return false, fmt.Errorf("replace targets missing key %q", key)
			}
		}
		existing, exists := parent[key]
		if exists && equalJSON(existing, op.Value) {
			return false, nil // idempotent write: no change
		}
		parent[key] = op.Value
		return true, nil
	case "remove":
		if _, ok := parent[key]; !ok {
			return false, nil // removing what is absent is a no-op, not an error
		}
		delete(parent, key)
		return true, nil
	default:
		return false, fmt.Errorf("unknown op %q", op.Op)
	}
}

// navigate walks tokens[:-1], creating intermediate maps when an "add" dives
// into a missing object segment, and returns the final container map and key.
func navigate(doc map[string]any, tokens []string) (map[string]any, string, error) {
	if len(tokens) == 0 {
		return nil, "", errors.New("cannot navigate to root")
	}
	cur := doc
	for _, t := range tokens[:len(tokens)-1] {
		next, ok := cur[t]
		if !ok {
			m := map[string]any{}
			cur[t] = m
			cur = m
			continue
		}
		switch n := next.(type) {
		case map[string]any:
			cur = n
		case []any:
			idx, err := strconv.Atoi(t)
			if err != nil || idx < 0 || idx >= len(n) {
				return nil, "", fmt.Errorf("array index %q out of range", t)
			}
			m, ok := n[idx].(map[string]any)
			if !ok {
				return nil, "", fmt.Errorf("segment %q is not an object", t)
			}
			cur = m
		default:
			return nil, "", fmt.Errorf("segment %q is not traversable", t)
		}
	}
	return cur, tokens[len(tokens)-1], nil
}

func equalJSON(a, b any) bool {
	ab, _ := json.Marshal(a)
	bb, _ := json.Marshal(b)
	return string(ab) == string(bb)
}

// DeepCopy returns a value-copied document so failed patch attempts leave no
// partial state behind. It copies structurally (without a JSON round trip) so
// integer values stay integers through multiple mutation passes.
func DeepCopy(doc map[string]any) map[string]any {
	out, _ := deepCopy(doc).(map[string]any)
	return out
}

func deepCopy(v any) any {
	switch t := v.(type) {
	case map[string]any:
		m := make(map[string]any, len(t))
		for k, val := range t {
			m[k] = deepCopy(val)
		}
		return m
	case []any:
		s := make([]any, len(t))
		for i := range t {
			s[i] = deepCopy(t[i])
		}
		return s
	default:
		return v
	}
}
