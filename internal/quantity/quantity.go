// Package quantity parses the small set of resource quantities used by the
// fixtures: CPU as millicores ("500m") or whole cores ("1", "0.25"), and
// memory in binary/decimal suffixes ("128Mi", "1Gi", "512M").
//
// It is independent of the admission package and has its own tests, since
// parsing mistakes must surface as input errors, not compute failures.
package quantity

import (
	"fmt"
	"strconv"
	"strings"
)

// ParseCPU converts a CPU quantity to an integer millicore count.
func ParseCPU(s string) (int, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, fmt.Errorf("empty cpu quantity")
	}
	if strings.HasSuffix(s, "m") {
		n, err := strconv.Atoi(s[:len(s)-1])
		if err != nil || n < 0 {
			return 0, fmt.Errorf("invalid millicore quantity %q", s)
		}
		return n, nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || f < 0 {
		return 0, fmt.Errorf("invalid cpu quantity %q", s)
	}
	milli := int(f*1000 + 0.5)
	return milli, nil
}

// ParseMemory converts a memory quantity to an integer byte count.
func ParseMemory(s string) (int64, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, fmt.Errorf("empty memory quantity")
	}
	// Longest suffixes first: Mi, Gi, Ti then M, G, T.
	suffixes := []struct {
		s string
		f int64
	}{
		{"Mi", 1 << 20}, {"Gi", 1 << 30}, {"Ti", 1 << 40},
		{"M", 1000 * 1000}, {"G", 1000 * 1000 * 1000}, {"T", 1000 * 1000 * 1000 * 1000},
	}
	for _, suf := range suffixes {
		if strings.HasSuffix(s, suf.s) {
			n, err := strconv.ParseInt(s[:len(s)-len(suf.s)], 10, 64)
			if err != nil || n < 0 {
				return 0, fmt.Errorf("invalid memory quantity %q", s)
			}
			return n * suf.f, nil
		}
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("invalid memory quantity %q", s)
	}
	return n, nil
}
