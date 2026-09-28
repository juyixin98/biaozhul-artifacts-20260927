// Package reference is the independent matching oracle. It deliberately uses
// the simplest possible per-subscription, per-filter algorithm — no index, no
// shared code with the trie kernel — so that differential tests compare two
// independent implementations of the same specification rather than one
// implementation against itself.
package reference

import (
	"sort"

	"topicrouter/internal/protocol"
)

// Hit is one winning match.
type Hit struct {
	SubscriberID string
	Filter       string
}

// Match scans EVERY frame and EVERY filter (a full table scan by design) and
// returns the set of matching subscribers with their single winning filter.
// Duplicate matches of one subscriber through several filters collapse to one
// Hit, exactly as the indexed engine must behave.
func Match(topic protocol.Topic, frames []protocol.Frame) []Hit {
	tl := topic.Levels()
	var hits []Hit
	for _, fr := range frames {
		var winner *protocol.Filter
		for i := range fr.Filters {
			f := &fr.Filters[i]
			if filterMatches(f.Levels(), tl) {
				if winner == nil || prefer(f, winner) {
					winner = f
				}
			}
		}
		if winner != nil {
			hits = append(hits, Hit{SubscriberID: fr.SubscriberID, Filter: winner.Raw()})
		}
	}
	sort.Slice(hits, func(i, j int) bool {
		if hits[i].SubscriberID != hits[j].SubscriberID {
			return hits[i].SubscriberID < hits[j].SubscriberID
		}
		return hits[i].Filter < hits[j].Filter
	})
	return hits
}

// filterMatches is the direct level-by-level definition.
func filterMatches(fl, tl []string) bool {
	for i, f := range fl {
		if f == protocol.MultiLevel {
			// '#' is only legal at the final level; it matches any number of
			// remaining topic levels, including zero.
			return true
		}
		if i >= len(tl) {
			// Exhausted the topic while the filter still has fixed levels.
			return false
		}
		if f == protocol.SingleLevel {
			// '+' matches exactly one level, INCLUDING an empty level.
			continue
		}
		if f != tl[i] {
			// Empty levels compare literally: "" == "" only.
			return false
		}
	}
	// All filter levels consumed: exact length match (a trailing '#' would
	// already have returned above).
	return len(fl) == len(tl)
}

// prefer reports whether candidate a should win over incumbent b for the same
// subscriber. This is the canonical tie-break the indexed engine must produce:
//
//  1. fewer wildcard levels ('+' and '#' each count once)
//  2. shorter filter (fewer total levels)
//  3. lexicographically smaller raw filter
func prefer(a, b *protocol.Filter) bool {
	aw, bw := wildcardCount(a.Levels()), wildcardCount(b.Levels())
	if aw != bw {
		return aw < bw
	}
	if a.Len() != b.Len() {
		return a.Len() < b.Len()
	}
	return a.Raw() < b.Raw()
}

func wildcardCount(levels []string) int {
	n := 0
	for _, l := range levels {
		if l == protocol.SingleLevel || l == protocol.MultiLevel {
			n++
		}
	}
	return n
}

// Scans returns the number of (subscriber, filter) pair examinations — the
// reference's analogue of the indexed engine's trie access count. For this
// oracle it is always the total number of filters across all frames: proof
// that it performs a full table scan.
func Scans(frames []protocol.Frame) int64 {
	var n int64
	for _, fr := range frames {
		n += int64(len(fr.Filters))
	}
	return n
}
