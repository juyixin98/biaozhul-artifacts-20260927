package domain

// MatchSelector reports whether labels satisfy every selector requirement.
// Selector changes are versioned by SelectorEpoch on the group; this function
// only answers the membership question for one concrete selector version.
func MatchSelector(selector, labels map[string]string) bool {
	if len(selector) == 0 {
		return false
	}
	for k, v := range selector {
		if labels[k] != v {
			return false
		}
	}
	return true
}
