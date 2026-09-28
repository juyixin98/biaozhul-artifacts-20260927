package netmodel

import (
	"fmt"
	"math/big"
	"strconv"
	"strings"
)

// ParsePortRange parses "lo-hi", a single number, or "*"/"any" for the full
// 16-bit port domain.
func ParsePortRange(tok string) (Int1D, error) {
	tok = strings.TrimSpace(tok)
	if tok == "*" || tok == "any" || tok == "" {
		return Universe1D(BitsPort), nil
	}
	lo, hi := tok, tok
	if i := strings.IndexByte(tok, '-'); i >= 0 {
		lo, hi = tok[:i], tok[i+1:]
	}
	l, err := strconv.Atoi(strings.TrimSpace(lo))
	if err != nil || l < 0 || l > 65535 {
		return Int1D{}, fmt.Errorf("invalid port bound %q", tok)
	}
	h, err := strconv.Atoi(strings.TrimSpace(hi))
	if err != nil || h < 0 || h > 65535 {
		return Int1D{}, fmt.Errorf("invalid port bound %q", tok)
	}
	if l > h {
		return Int1D{}, fmt.Errorf("inverted port range %q", tok)
	}
	return MustRange1D(BitsPort, big.NewInt(int64(l)), big.NewInt(int64(h))), nil
}
