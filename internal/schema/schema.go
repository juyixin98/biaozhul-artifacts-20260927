// Package schema declares field-level merge semantics per resource kind.
//
// Only lists need explicit declarations:
//
//   - atomic: the whole list is owned by whoever last wrote it (replacement).
//   - set:    unordered collection of scalars; ownership is per scalar value,
//     no order is preserved (output is sorted/deduplicated).
//   - map:    list of objects identified by a key field ("k8s listType=map");
//     ownership is per element field.
//
// Objects not mentioned are deep-merged field by field. Nested declaration
// paths use dotted field names relative to the resource (or relative inside a
// keyed list: keys are not part of the schema path).
package schema

import (
	"sort"
	"strings"

	"fieldmerge/internal/apperr"
)

type ListType string

const (
	ListAtomic ListType = "atomic"
	ListSet    ListType = "set"
	ListMap    ListType = "map"
)

// ListDecl declares one list field.
type ListDecl struct {
	Type    ListType `json:"type"`
	KeyName string   `json:"key,omitempty"` // required (and only allowed) for map
}

// Schema is the semantic declaration for a resource kind.
type Schema struct {
	Kind  string              `json:"kind"`
	Lists map[string]ListDecl `json:"lists"`
}

// New validates and builds a Schema.
func New(kind string, decls map[string]ListDecl) (*Schema, error) {
	s := &Schema{Kind: kind, Lists: map[string]ListDecl{}}
	for path, d := range decls {
		p := strings.Trim(path, ".")
		if p == "" {
			return nil, apperr.New(apperr.InvalidInput, "schema_bad_path", "empty list declaration path")
		}
		switch d.Type {
		case ListAtomic, ListSet:
			if d.KeyName != "" {
				return nil, apperr.New(apperr.InvalidInput, "schema_bad_decl",
					"key is only allowed for map lists (path %q)", path)
			}
		case ListMap:
			if d.KeyName == "" {
				return nil, apperr.New(apperr.InvalidInput, "schema_bad_decl",
					"map list %q requires a key", path)
			}
		default:
			return nil, apperr.New(apperr.InvalidInput, "schema_bad_list_type",
				"unknown list type %q at %q", d.Type, path)
		}
		s.Lists[p] = d
	}
	return s, nil
}

// Lookup returns the list declaration whose dotted path equals fields, if any.
func (s *Schema) Lookup(fields []string) (ListDecl, bool) {
	if s == nil {
		return ListDecl{}, false
	}
	d, ok := s.Lists[strings.Join(fields, ".")]
	return d, ok
}

// ListPaths returns declared paths sorted, for diagnostics.
func (s *Schema) ListPaths() []string {
	out := make([]string, 0, len(s.Lists))
	for p := range s.Lists {
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}
