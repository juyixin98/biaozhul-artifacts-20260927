// Package fieldpath defines canonical field-set path notation used for
// ownership tracking and conflict reports.
//
// Three segment kinds exist:
//
//	.foo              struct/map field
//	[name="edge-1"]   keyed-list element key (value stored as a JSON token)
//	[^"tag-x"]        set element value (JSON token)
//
// Example: .ingresses[name="edge-1"].host, .tags[^"canary"].
//
// Atomic lists have no per-element ownership: the claim is recorded on the
// list path itself, so index segments are intentionally absent.
package fieldpath

import (
	"encoding/json"
	"fmt"
	"strings"
)

type Kind uint8

const (
	Field Kind = iota
	Key
	SetElem
)

// Segment is one path component. For Key/SetElem, Token holds the JSON
// encoding of the scalar value (quoted for strings: "edge-1", 8080, true).
type Segment struct {
	Kind  Kind
	Name  string // field name, or key field name for Key
	Token string
}

// Path is a canonical field-set path; the empty path denotes the resource root.
type Path []Segment

func FieldPath(names ...string) Path {
	p := make(Path, 0, len(names))
	for _, n := range names {
		p = append(p, Segment{Kind: Field, Name: n})
	}
	return p
}

func (p Path) PushField(name string) Path {
	out := make(Path, len(p)+1)
	copy(out, p)
	out[len(p)] = Segment{Kind: Field, Name: name}
	return out
}

func (p Path) PushKey(name, token string) Path {
	out := make(Path, len(p)+1)
	copy(out, p)
	out[len(p)] = Segment{Kind: Key, Name: name, Token: token}
	return out
}

func (p Path) PushSet(token string) Path {
	out := make(Path, len(p)+1)
	copy(out, p)
	out[len(p)] = Segment{Kind: SetElem, Token: token}
	return out
}

func (p Path) Equal(q Path) bool {
	if len(p) != len(q) {
		return false
	}
	for i := range p {
		if p[i] != q[i] {
			return false
		}
	}
	return true
}

// HasPrefix reports whether p is under (or equal to) prefix.
func (p Path) HasPrefix(prefix Path) bool {
	if len(p) < len(prefix) {
		return false
	}
	for i := range prefix {
		if p[i] != prefix[i] {
			return false
		}
	}
	return true
}

func (p Path) String() string {
	var b strings.Builder
	for i, s := range p {
		switch s.Kind {
		case Field:
			if i > 0 {
				b.WriteByte('.')
			}
			b.WriteString(s.Name)
		case Key:
			fmt.Fprintf(&b, "[%s=%s]", s.Name, s.Token)
		case SetElem:
			b.WriteString("[^")
			b.WriteString(s.Token)
			b.WriteByte(']')
		}
	}
	return b.String()
}

// MarshalText implements encoding.TextMarshaler.
func (p Path) MarshalText() ([]byte, error) { return []byte(p.String()), nil }

// UnmarshalText parses the canonical notation.
func (p *Path) UnmarshalText(data []byte) error {
	q, err := Parse(string(data))
	if err != nil {
		return err
	}
	*p = q
	return nil
}

// Parse decodes a canonical path. Grammar:
//
//	path    = firstField*( segment )
//	segment = field | key | set
//	field   = "." NAME
//	key     = "[" NAME "=" jsonScalar "]"
//	set     = "[^" jsonScalar "]"
//
// The first field segment may omit the leading dot.
func Parse(s string) (Path, error) {
	if s == "" {
		return Path{}, nil
	}
	var p Path
	i := 0
	if s[0] != '[' {
		j := strings.IndexAny(s, ".[]")
		if j < 0 {
			j = len(s)
		}
		name := s[:j]
		if !validFieldName(name) {
			return nil, fmt.Errorf("invalid field name %q", name)
		}
		p = append(p, Segment{Kind: Field, Name: name})
		i = j
	}
	for i < len(s) {
		switch s[i] {
		case '.':
			i++
			j := i
			for j < len(s) && s[j] != '.' && s[j] != '[' {
				j++
			}
			name := s[i:j]
			if !validFieldName(name) {
				return nil, fmt.Errorf("invalid field name %q", name)
			}
			p = append(p, Segment{Kind: Field, Name: name})
			i = j
		case '[':
			kind := Key
			content := i + 1
			if content < len(s) && s[content] == '^' {
				kind = SetElem
				content++
			}
			var namePart string
			if kind == Key {
				eq := scanUnquoted(s, content, '=')
				if eq < 0 {
					return nil, fmt.Errorf("malformed key segment at byte %d", i)
				}
				namePart = s[content:eq]
				if !validFieldName(namePart) {
					return nil, fmt.Errorf("invalid key field name %q", namePart)
				}
				content = eq + 1
			}
			end := scanJSONTokenEnd(s, content)
			if end < 0 || end >= len(s) || s[end] != ']' {
				return nil, fmt.Errorf("malformed segment at byte %d", i)
			}
			tok := s[content:end]
			var probe any
			if err := json.Unmarshal([]byte(tok), &probe); err != nil {
				return nil, fmt.Errorf("invalid scalar token %q: %w", tok, err)
			}
			if !isScalar(probe) {
				return nil, fmt.Errorf("token %q is not scalar", tok)
			}
			seg := Segment{Kind: kind, Token: tok}
			if kind == Key {
				seg.Name = namePart
			}
			p = append(p, seg)
			i = end + 1
		default:
			return nil, fmt.Errorf("unexpected %q at byte %d", s[i], i)
		}
	}
	return p, nil
}

func validFieldName(n string) bool {
	if n == "" {
		return false
	}
	for _, r := range n {
		if r == '.' || r == '[' || r == ']' || r == '=' {
			return false
		}
	}
	return true
}

// scanUnquoted returns the index of the first occurrence of b outside a JSON
// string literal, or -1.
func scanUnquoted(s string, from int, b byte) int {
	inStr := false
	for i := from; i < len(s); i++ {
		c := s[i]
		if inStr {
			if c == '\\' {
				i++
				continue
			}
			if c == '"' {
				inStr = false
			}
			continue
		}
		if c == '"' {
			inStr = true
		} else if c == b {
			return i
		}
	}
	return -1
}

// scanJSONTokenEnd returns the index immediately after the JSON scalar token
// starting at from (a quoted string is handled quote-aware).
func scanJSONTokenEnd(s string, from int) int {
	if from >= len(s) {
		return -1
	}
	if s[from] == '"' {
		for i := from + 1; i < len(s); i++ {
			if s[i] == '\\' {
				i++
				continue
			}
			if s[i] == '"' {
				return i + 1
			}
		}
		return -1
	}
	i := from
	for i < len(s) && s[i] != ']' {
		i++
	}
	return i
}

// Token returns the canonical JSON token for a set/key scalar value.
func Token(v any) (string, error) {
	if !isScalar(v) {
		return "", fmt.Errorf("non-scalar key/set value: %T", v)
	}
	b, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

func isScalar(v any) bool {
	switch v.(type) {
	case string, float64, bool, nil:
		return true
	}
	return false
}

// Lookup follows p through a decoded JSON tree. Key segments match keyed-list
// elements by their key field; SetElem segments match by scalar token.
func Lookup(tree any, p Path) (any, bool) {
	cur := tree
	for _, seg := range p {
		switch seg.Kind {
		case Field:
			m, ok := cur.(map[string]any)
			if !ok {
				return nil, false
			}
			cur, ok = m[seg.Name]
			if !ok {
				return nil, false
			}
		case Key:
			list, ok := cur.([]any)
			if !ok {
				return nil, false
			}
			found := false
			for _, el := range list {
				m, ok := el.(map[string]any)
				if !ok {
					continue
				}
				t, err := Token(m[seg.Name])
				if err != nil || t != seg.Token {
					continue
				}
				cur = el
				found = true
				break
			}
			if !found {
				return nil, false
			}
		case SetElem:
			list, ok := cur.([]any)
			if !ok {
				return nil, false
			}
			found := false
			for _, el := range list {
				t, err := Token(el)
				if err != nil {
					continue
				}
				if t == seg.Token {
					cur = el
					found = true
					break
				}
			}
			if !found {
				return nil, false
			}
		}
	}
	return cur, true
}
