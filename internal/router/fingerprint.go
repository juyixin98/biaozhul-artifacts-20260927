package router

import (
	"fmt"
	"sort"
	"strings"

	"flowrouter/internal/config"
)

// fingerprint renders the exact routing-relevant inputs (identity, weight,
// up/down) as a stable string. Addresses are intentionally excluded: changing
// an address does not change bucket ownership and must not force a migration.
// Order-independent: members are sorted before rendering.
func fingerprint(ids []string, byID map[string]config.Member, down map[string]bool) string {
	ord := append([]string(nil), ids...)
	sort.Strings(ord)
	var b strings.Builder
	for _, id := range ord {
		m := byID[id]
		d := 0
		if down[id] {
			d = 1
		}
		fmt.Fprintf(&b, "%s:%d:%d;", id, m.Weight, d)
	}
	return b.String()
}
