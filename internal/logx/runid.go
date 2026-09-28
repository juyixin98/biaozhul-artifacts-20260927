package logx

import (
	"crypto/rand"
	"encoding/hex"
	"os"
	"strconv"
	"time"
)

// NewRunID generates a run identifier correlated across log lines, stored
// events and HTTP responses. Format: r-<unixmillis>-<6 random hex> with the
// process id appended when available, so concurrent local runs are still
// distinguishable.
func NewRunID() string {
	var b [6]byte
	if _, err := rand.Read(b[:]); err != nil {
		// rand.Read failing is extraordinary; fall back to time-derived
		// entropy rather than returning an empty/unknown identity.
		return "r-" + strconv.FormatInt(time.Now().UnixNano(), 16) + "-fallback"
	}
	host, _ := os.Hostname()
	if host == "" {
		host = "local"
	}
	return "r-" + strconv.FormatInt(time.Now().UnixMilli(), 10) + "-" + hex.EncodeToString(b[:]) + "-" + host
}
