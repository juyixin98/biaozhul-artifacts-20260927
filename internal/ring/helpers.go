package ring

import (
	"sort"

	"flowrouter/internal/flow"
	"flowrouter/internal/hashx"
)

// flowHash is the single place the ring turns a tuple into its ring ordinal.
// Keeping this thin indirection in the ring package means a future hash
// version change for routing is changed once.
func flowHash(f flow.FiveTuple) uint64 {
	return hashx.FlowHash(f.CanonicalKey())
}

// LookupFlow is a convenience wrapper around Lookup for tuple inputs.
func (r *Ring) LookupFlow(f flow.FiveTuple) (string, bool) {
	return r.Lookup(flowHash(f))
}

func sortStrings(s []string) { sort.Strings(s) }
