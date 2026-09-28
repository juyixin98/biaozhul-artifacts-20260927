package actualserver

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
)

// requestIDHeader is the correlation header used by both planes.
const requestIDHeader = "X-Request-ID"

// ensureRequestID reads the inbound X-Request-ID or generates one, and sets
// it on the response so callers can quote it.
func ensureRequestID(w http.ResponseWriter, r *http.Request) string {
	rid := r.Header.Get(requestIDHeader)
	if rid == "" {
		rid = newRequestID()
	}
	w.Header().Set(requestIDHeader, rid)
	return rid
}

func newRequestID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return "req-" + hex.EncodeToString(b[:])
}
