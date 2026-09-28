// Package reference is an intentionally simple, independent per-subscription
// matcher used as the oracle in differential tests.
//
// It is deliberately NOT built from the kernel: matching is a direct recursive
// comparison of the already-split layer slices, written independently from the
// trie implementation. The kernel may be subtly wrong in a way that shares a
// root cause with itself; this oracle exists so that cannot happen. Expected
// outputs in golden tests are hand-written as well (see testdata/golden), not
// produced by either implementation.
package reference

import (
	"sort"

	"topicrouter/internal/topic"
)

// MatchOne decides whether filterLayers matches topicLayers.
//
// Semantics, from the protocol spec:
//
//   - equal length literal layers match layer by layer;
//   - a "+" filter layer matches exactly one topic layer, including "";
//   - a "#" filter layer, only legal at the end, matches zero or more
//     remaining topic layers (including sequences that contain "");
//   - every other filter layer must equal the topic layer exactly.
func MatchOne(filterLayers, topicLayers []string) bool {
	f, t := filterLayers, topicLayers
	for fi := 0; fi < len(f); fi++ {
		switch f[fi] {
		case "#":
			// Legal only as the final layer; callers validate filters, but the
			// oracle defends itself: a non-terminal '#' never matches.
			return fi == len(f)-1
		case "+":
			if len(t) == 0 {
				return false
			}
		default:
			if len(t) == 0 || t[0] != f[fi] {
				return false
			}
		}
		// Any non-"#" filter layer consumes exactly one topic layer.
		t = t[1:]
	}
	return len(t) == 0
}

// MatchFilter is the string-convenience wrapper that validates inputs first.
func MatchFilter(filter, topicText string) (bool, error) {
	if err := topic.ValidateFilter(filter); err != nil {
		return false, err
	}
	if err := topic.ValidateTopic(topicText); err != nil {
		return false, err
	}
	return MatchOne(topic.SplitSubject(filter), topic.SplitSubject(topicText)), nil
}

// MustMatchFilter is MatchFilter that panics on invalid input, for fixtures
// that are validated separately.
func MustMatchFilter(filter, topicText string) bool {
	ok, err := MatchFilter(filter, topicText)
	if err != nil {
		panic(err)
	}
	return ok
}

// RouteAll evaluates every (id, filter) pair against one topic and returns the
// sorted set of matching ids — the reference "full table scan" baseline that
// tests compare against the indexed kernel result.
func RouteAll(filters map[string]string, topicText string) ([]string, error) {
	if err := topic.ValidateTopic(topicText); err != nil {
		return nil, err
	}
	tl := topic.SplitSubject(topicText)
	var matched []string
	for id, f := range filters {
		if err := topic.ValidateFilter(f); err != nil {
			return nil, err
		}
		if MatchOne(topic.SplitSubject(f), tl) {
			matched = append(matched, id)
		}
	}
	return sortStrings(matched), nil
}

func sortStrings(s []string) []string {
	sort.Strings(s)
	return s
}
