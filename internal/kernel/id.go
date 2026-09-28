package kernel

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
)

// NewID returns a 128-bit random hex identifier with the given prefix
// (e.g. "msg_", "rcp_", "att_", "evt_"). Randomness comes from crypto/rand so
// ids are unguessable and unique across local runs without a coordinator.
func NewID(prefix string) string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is not a condition under which the broker may
		// continue issuing ids; surface it as a panic at startup/request time
		// rather than silently collapsing ids.
		panic("kernel: crypto/rand failed: " + err.Error())
	}
	return prefix + hex.EncodeToString(b[:])
}

// DeterministicID derives a stable id from parts, used by tests and replay
// tooling where a repeatable id is more readable than a random one.
func DeterministicID(prefix string, parts ...string) string {
	h := sha256.New()
	for _, p := range parts {
		h.Write([]byte(p))
		h.Write([]byte{0})
	}
	return prefix + hex.EncodeToString(h.Sum(nil))[:32]
}
