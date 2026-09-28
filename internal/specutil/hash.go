// Package specutil provides canonical hashing of an object spec.
package specutil

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
)

// Hash returns a stable SHA-256 hash of a spec map. Map keys are sorted
// recursively so two maps that differ only in key order hash identically.
// A nil/empty map hashes to the empty-string hash.
func Hash(spec map[string]any) string {
	canonical := canonicalize(spec)
	b, _ := json.Marshal(canonical)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// canonicalize rebuilds maps with sorted keys and normalizes []any elements.
func canonicalize(v any) any {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		out := make([][2]any, 0, len(keys))
		for _, k := range keys {
			out = append(out, [2]any{k, canonicalize(t[k])})
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i := range t {
			out[i] = canonicalize(t[i])
		}
		return out
	default:
		return v
	}
}
