// Package model defines the data and error contracts shared by the merge
// engine, storage, coordination loop and HTTP adapter.
package model

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// -----------------------------------------------------------------------------
// Field paths
//
// A field path is the address of an owned leaf inside a resource. It is a
// sequence of segments:
//
//	FieldSeg("spec")              -> .spec
//	FieldSeg("replicas")          -> .spec.replicas
//	KeySeg("name", `"web"`)       -> .containers[name="web"].port
//	KeySeg(SetElementKey, `"v2"`) -> .tags[_="v2"]
//
// The Value carried by a key segment is always JSON-encoded text so that the
// path string is unambiguous. String renders are the canonical storage form
// (SQLite ownership rows, history, conflict reports, tests).
// -----------------------------------------------------------------------------

type SegKind int

const (
	SegField SegKind = iota
	SegKey
)

// SetElementKey is the synthetic key-field used for set-list element leaves.
// A keyed list must not declare this string as its real key field.
const SetElementKey = "_"

type Seg struct {
	Kind  SegKind
	Field string // SegField: object member name
	Key   string // SegKey:  key field name ("_" for set elements)
	Value string // SegKey:  JSON-encoded key value
}

func FieldSeg(name string) Seg { return Seg{Kind: SegField, Field: name} }

func KeySeg(keyField string, jsonValue string) Seg {
	return Seg{Kind: SegKey, Key: keyField, Value: jsonValue}
}

type Path []Seg

func (p Path) Child(seg Seg) Path {
	child := make(Path, 0, len(p)+1)
	child = append(child, p...)
	child = append(child, seg)
	return child
}

var identRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_-]*$`)

func (p Path) String() string {
	var b strings.Builder
	for _, s := range p {
		switch s.Kind {
		case SegField:
			if identRe.MatchString(s.Field) {
				b.WriteByte('.')
				b.WriteString(s.Field)
			} else {
				raw, _ := json.Marshal(s.Field)
				b.WriteByte('[')
				b.Write(raw)
				b.WriteByte(']')
			}
		case SegKey:
			b.WriteByte('[')
			b.WriteString(s.Key)
			b.WriteByte('=')
			b.WriteString(s.Value)
			b.WriteByte(']')
		}
	}
	return b.String()
}

// HasPathPrefix reports whether owned path key is equal to prefix or nested
// underneath it (segment-boundary aware: ".a" does not match ".ab").
func HasPathPrefix(key, prefix string) bool {
	if key == prefix {
		return true
	}
	if !strings.HasPrefix(key, prefix) {
		return false
	}
	next := key[len(prefix)]
	return next == '.' || next == '['
}

// -----------------------------------------------------------------------------
// Field sets and ownership
// -----------------------------------------------------------------------------

// FieldSet is a set of canonical field paths.
type FieldSet map[string]struct{}

func (fs FieldSet) Add(p Path) { fs[p.String()] = struct{}{} }

func (fs FieldSet) Has(p Path) bool { _, ok := fs[p.String()]; return ok }

func (fs FieldSet) Delete(p Path) { delete(fs, p.String()) }

// Keys returns the sorted members; field-path strings sort deterministically.
func (fs FieldSet) Keys() []string {
	out := make([]string, 0, len(fs))
	for k := range fs {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func (fs FieldSet) Clone() FieldSet {
	out := make(FieldSet, len(fs))
	for k := range fs {
		out[k] = struct{}{}
	}
	return out
}

// Owners maps a canonical field path to the set of field managers that own a
// share of it. A leaf shared by two managers (identical value) lists both.
type Owners map[string]map[string]struct{}

func (o Owners) Add(path, manager string) {
	set, ok := o[path]
	if !ok {
		set = map[string]struct{}{}
		o[path] = set
	}
	set[manager] = struct{}{}
}

// Remove drops manager from path and reports whether no owners remain.
func (o Owners) Remove(path, manager string) bool {
	set, ok := o[path]
	if !ok {
		return true
	}
	delete(set, manager)
	if len(set) == 0 {
		delete(o, path)
		return true
	}
	return false
}

func (o Owners) Managers(path string) []string {
	out := make([]string, 0, len(o[path]))
	for m := range o[path] {
		out = append(out, m)
	}
	sort.Strings(out)
	return out
}

// Others returns the sorted co-owners of path excluding manager.
func (o Owners) Others(path, manager string) []string {
	out := []string{}
	for m := range o[path] {
		if m != manager {
			out = append(out, m)
		}
	}
	sort.Strings(out)
	return out
}

// DropSubtree removes every owned path equal to or below prefix. Used when an
// atomic list is replaced wholesale and on forced structural collisions.
func (o Owners) DropSubtree(prefix string) {
	for k := range o {
		if HasPathPrefix(k, prefix) {
			delete(o, k)
		}
	}
}

func (o Owners) Clone() Owners {
	out := make(Owners, len(o))
	for k, v := range o {
		cp := make(map[string]struct{}, len(v))
		for m := range v {
			cp[m] = struct{}{}
		}
		out[k] = cp
	}
	return out
}

// -----------------------------------------------------------------------------
// Collection schema
// -----------------------------------------------------------------------------

type ListKind int

const (
	// ListAtomic (default): the whole array is one owned leaf. Any change
	// replaces the array; co-owners with a different payload conflict.
	ListAtomic ListKind = iota
	// ListSet: array of scalars compared by membership; each element is an
	// independently owned leaf addressed through SetElementKey.
	ListSet
	// ListKeyed: array of objects identified by a scalar key field; elements
	// with the same key merge field by field.
	ListKeyed
)

func (k ListKind) String() string {
	switch k {
	case ListSet:
		return "set"
	case ListKeyed:
		return "keyed"
	default:
		return "atomic"
	}
}

// MarshalJSON / UnmarshalJSON accept the friendly strings "atomic", "set",
// "keyed" in API payloads while remaining integers internally.
func (k ListKind) MarshalJSON() ([]byte, error) {
	return json.Marshal(k.String())
}

func (k *ListKind) UnmarshalJSON(raw []byte) error {
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		switch s {
		case "", "atomic":
			*k = ListAtomic
		case "set":
			*k = ListSet
		case "keyed":
			*k = ListKeyed
		default:
			return fmt.Errorf("unknown list kind %q (want atomic|set|keyed)", s)
		}
		return nil
	}
	var n int
	if err := json.Unmarshal(raw, &n); err != nil {
		return fmt.Errorf("list kind must be a string (atomic|set|keyed) or number: %w", err)
	}
	if n < 0 || n > 2 {
		return fmt.Errorf("invalid numeric list kind %d", n)
	}
	*k = ListKind(n)
	return nil
}

// Schema declares collection semantics at array paths. Paths are canonical
// path strings (as produced by Path.String). Absence means atomic.
type Schema struct {
	Lists map[string]ListKind `json:"lists,omitempty"`
	// Keys names the key field for keyed lists; defaults to "name".
	Keys map[string]string `json:"keys,omitempty"`
}

// KindAt returns the list kind and key field declared for array path p.
func (s Schema) KindAt(p Path) (ListKind, string) {
	key := p.String()
	kind, ok := s.Lists[key]
	if !ok {
		return ListAtomic, ""
	}
	keyField := s.Keys[key]
	if keyField == "" {
		keyField = "name"
	}
	return kind, keyField
}

// Validate checks the schema itself: supported kinds, key names and that
// declared paths point at arrays in the sample document.
func (s Schema) Validate(sample any) error {
	for path, kind := range s.Lists {
		if kind != ListAtomic && kind != ListSet && kind != ListKeyed {
			return fmt.Errorf("schema %q: unknown list kind %d", path, kind)
		}
		if kind == ListKeyed {
			kf := s.Keys[path]
			if kf == SetElementKey {
				return fmt.Errorf("schema %q: key field %q is reserved", path, SetElementKey)
			}
		}
		if sample != nil {
			v, ok := lookupPath(sample, path)
			if ok {
				if _, isArr := v.([]any); !isArr {
					return fmt.Errorf("schema %q: path is not an array", path)
				}
			}
		}
	}
	return nil
}

// lookupPath is used only for schema validation; it understands the canonical
// renders for field and simple key segments.
func lookupPath(doc any, rendered string) (any, bool) {
	cur := doc
	for len(rendered) > 0 {
		switch rendered[0] {
		case '.':
			i := 1
			for i < len(rendered) && rendered[i] != '.' && rendered[i] != '[' {
				i++
			}
			name := rendered[1:i]
			obj, ok := cur.(map[string]any)
			if !ok {
				return nil, false
			}
			cur, ok = obj[name]
			if !ok {
				return nil, false
			}
			rendered = rendered[i:]
		case '[':
			end := strings.IndexByte(rendered, ']')
			if end < 0 {
				return nil, false
			}
			inner := rendered[1:end]
			eq := strings.IndexByte(inner, '=')
			if eq < 0 {
				return nil, false
			}
			var want json.RawMessage
			if err := json.Unmarshal([]byte(inner[eq+1:]), &want); err != nil {
				return nil, false
			}
			arr, ok := cur.([]any)
			if !ok {
				return nil, false
			}
			keyField := inner[:eq]
			found := false
			for _, e := range arr {
				if eo, ok := e.(map[string]any); ok {
					if ev, ok := eo[keyField]; ok && jsonEqualLoose(ev, want) {
						cur = e
						found = true
						break
					}
				}
			}
			if !found {
				return nil, false
			}
			rendered = rendered[end+1:]
		default:
			return nil, false
		}
	}
	return cur, true
}

func jsonEqualLoose(v any, raw json.RawMessage) bool {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	_ = enc.Encode(v)
	return bytes.Equal(bytes.TrimSpace(b.Bytes()), bytes.TrimSpace(raw))
}

// -----------------------------------------------------------------------------
// Values
// -----------------------------------------------------------------------------

// DecodeValue decodes JSON with json.Number so integer magnitudes and digit
// forms survive the round trip into conflict reports and storage.
func DecodeValue(raw []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return v, nil
}

// MustRaw re-encodes a decoded value; failures are internal invariants.
func MustRaw(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Sprintf("model: cannot marshal %T: %v", v, err))
	}
	return b
}

// -----------------------------------------------------------------------------
// Error contract
// -----------------------------------------------------------------------------

type Category string

const (
	CatInvalidInput       Category = "invalid_input"
	CatStateConflict      Category = "state_conflict"
	CatResourceExhausted  Category = "resource_exhausted"
	CatComputationFailure Category = "computation_failure"
	CatNotFound           Category = "not_found"
)

// Conflict describes one field that an apply could not claim.
type Conflict struct {
	Path    string          `json:"path"`
	Owners  []string        `json:"owners"`
	Current json.RawMessage `json:"current"`
	Applied json.RawMessage `json:"applied"`
}

// Error is the structured error exchanged between all layers.
type Error struct {
	Category  Category   `json:"category"`
	Code      string     `json:"code"`
	Message   string     `json:"message"`
	Conflicts []Conflict `json:"conflicts,omitempty"`
	Cause     error      `json:"-"`
}

func (e *Error) Error() string {
	if len(e.Conflicts) > 0 {
		paths := make([]string, len(e.Conflicts))
		for i, c := range e.Conflicts {
			paths[i] = c.Path
		}
		return fmt.Sprintf("%s/%s: %s (conflicts: %s)", e.Category, e.Code, e.Message, strings.Join(paths, ", "))
	}
	return fmt.Sprintf("%s/%s: %s", e.Category, e.Code, e.Message)
}

func (e *Error) Unwrap() error { return e.Cause }

func IsCategory(err error, c Category) bool {
	se, ok := err.(*Error)
	return ok && se.Category == c
}

// AsError extracts a structured *Error from err.
func AsError(err error) (*Error, bool) {
	se, ok := err.(*Error)
	return se, ok
}

// Change is one leaf-level difference between two live generations.
type Change struct {
	Path string          `json:"path"`
	Old  json.RawMessage `json:"old,omitempty"`
	New  json.RawMessage `json:"new,omitempty"`
}

// ChangeSet is the auditable diff attached to a history entry.
type ChangeSet struct {
	Added   []Change `json:"added,omitempty"`
	Changed []Change `json:"changed,omitempty"`
	Removed []Change `json:"removed,omitempty"`
}
