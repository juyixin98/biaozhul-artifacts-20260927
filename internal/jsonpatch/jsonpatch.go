// Package jsonpatch implements the small, explicit subset of RFC 6901/6902
// pointer handling and patch application used by the admission chain.
//
// The implementation is deliberately independent of the admission package and
// carries its own tests: patches are the core data/error contract between the
// pipeline and every mutator, so pointer semantics are unit-tested directly
// rather than only through admission scenarios.
package jsonpatch

import (
	"fmt"
	"strconv"
	"strings"

	"admission/internal/types"
)

// Pointer is a parsed RFC 6901 JSON pointer.
type Pointer struct {
	tokens []string
}

// Parse parses an RFC 6901 pointer. The empty pointer "" denotes the whole
// document and is not accepted by this pipeline (mutators edit members).
func Parse(p string) (Pointer, error) {
	if p == "" {
		return Pointer{}, fmt.Errorf("empty pointer is not supported")
	}
	if !strings.HasPrefix(p, "/") {
		return Pointer{}, fmt.Errorf("pointer %q must start with '/'", p)
	}
	raw := strings.Split(p[1:], "/")
	tokens := make([]string, 0, len(raw))
	for _, t := range raw {
		if t == "" {
			return Pointer{}, fmt.Errorf("pointer %q contains an empty token", p)
		}
		if strings.ContainsAny(t, "\\\x00") {
			return Pointer{}, fmt.Errorf("pointer %q contains an illegal escape", p)
		}
		// RFC 6901 unescaping: '~1' -> '/', '~0' -> '~' (order matters).
		t = strings.ReplaceAll(t, "~1", "/")
		t = strings.ReplaceAll(t, "~0", "~")
		tokens = append(tokens, t)
	}
	return Pointer{tokens: tokens}, nil
}

// String re-encodes the pointer.
func (p Pointer) String() string {
	if len(p.tokens) == 0 {
		return ""
	}
	var b strings.Builder
	for _, t := range p.tokens {
		b.WriteByte('/')
		b.WriteString(strings.ReplaceAll(strings.ReplaceAll(t, "~", "~0"), "/", "~1"))
	}
	return b.String()
}

// Tokens returns a copy of the pointer tokens.
func (p Pointer) Tokens() []string {
	out := make([]string, len(p.tokens))
	copy(out, p.tokens)
	return out
}

// IsBelow reports whether the pointer is at or below one of the allowed
// prefixes. An exact match is allowed; the next token under the prefix is
// allowed too. Example: prefix "/spec" allows "/spec/replicas".
func (p Pointer) IsBelow(allowed []string) bool {
	for _, a := range allowed {
		ap, err := Parse(a)
		if err != nil {
			continue
		}
		if len(p.tokens) < len(ap.tokens) {
			continue
		}
		equal := true
		for i := range ap.tokens {
			if p.tokens[i] != ap.tokens[i] {
				equal = false
				break
			}
		}
		if equal {
			return true
		}
	}
	return false
}

// child navigates from v through all but the last token.
func child(v any, tokens []string) (any, error) {
	cur := v
	for i, t := range tokens {
		m, ok := cur.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("path through %q: parent is not an object", strings.Join(tokens[:i+1], "/"))
		}
		next, present := m[t]
		if !present {
			return nil, &MissingParentError{Path: "/" + strings.Join(tokens[:i+1], "/")}
		}
		cur = next
	}
	return cur, nil
}

// MissingParentError means the target's container did not exist.
type MissingParentError struct{ Path string }

func (e *MissingParentError) Error() string { return "missing parent at " + e.Path }

// Apply executes one patch operation against a generic JSON tree and returns a
// new tree; the input is never mutated in place (the pipeline deep-copies
// anyway, but Apply is safe by construction).
//
// The root must be a map. Arrays are not produced by the resource model, so
// array index tokens are rejected rather than silently coerced.
func Apply(root any, op types.PatchOp) (any, error) {
	ptr, err := Parse(op.Path)
	if err != nil {
		return nil, err
	}
	toks := ptr.Tokens()
	if len(toks) == 0 {
		return nil, fmt.Errorf("patching the document root is not allowed")
	}
	if toks[0] == "-" || isIndex(toks[len(toks)-1]) {
		return nil, fmt.Errorf("array indices are not supported: %q", op.Path)
	}

	// Round-trip through JSON so op.Value carries the same concrete types a
	// decoded document has (float64/[]interface{}/map[string]interface{}).
	if op.Op != types.OpRemove {
		norm, err := normalizeValue(op.Value)
		if err != nil {
			return nil, fmt.Errorf("patch %s %s: bad value: %w", op.Op, op.Path, err)
		}
		op.Value = norm
	}

	clone, err := deepCopy(root)
	if err != nil {
		return nil, err
	}
	parentAny, err := child(clone, toks[:len(toks)-1])
	if err != nil {
		return nil, err
	}
	parent, ok := parentAny.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("parent of %q is not an object", op.Path)
	}
	key := toks[len(toks)-1]
	_, exists := parent[key]

	switch op.Op {
	case types.OpAdd:
		// 'add' to an object member creates or replaces.
		parent[key] = op.Value
	case types.OpReplace:
		if !exists {
			return nil, &MissingTargetError{Path: op.Path}
		}
		parent[key] = op.Value
	case types.OpRemove:
		if !exists {
			return nil, &MissingTargetError{Path: op.Path}
		}
		delete(parent, key)
	default:
		return nil, fmt.Errorf("unsupported op %q", op.Op)
	}
	return clone, nil
}

// MissingTargetError means replace/remove targeted a member that is absent.
type MissingTargetError struct{ Path string }

func (e *MissingTargetError) Error() string {
	return "target member does not exist: " + e.Path
}

func isIndex(t string) bool {
	if t == "" {
		return false
	}
	if t == "0" {
		return true
	}
	if t[0] == '0' {
		return false
	}
	_, err := strconv.Atoi(t)
	return err == nil
}

// normalizeValue round-trips a Go value through JSON so nested types match the
// decoded document.
func normalizeValue(v any) (any, error) {
	b, err := types.CanonicalJSON(v)
	if err != nil {
		return nil, err
	}
	var out any
	if err := jsonUnmarshal(b, &out); err != nil {
		return nil, err
	}
	return out, nil
}
